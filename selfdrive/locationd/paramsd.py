#!/usr/bin/env python3
import os
import math
import numpy as np

import cereal.messaging as messaging
from cereal import car, log
from openpilot.common.params import Params
from openpilot.common.realtime import config_realtime_process, DT_MDL
from openpilot.selfdrive.locationd.models.car_kf import CarKalman, ObservationKind, States
from openpilot.selfdrive.locationd.models.constants import GENERATED_DIR
from openpilot.selfdrive.locationd.helpers import PoseCalibrator, Pose
from openpilot.common.swaglog import cloudlog


MAX_ANGLE_OFFSET_DELTA = 20 * DT_MDL  # Max 20 deg/s
ROLL_MAX_DELTA = math.radians(20.0) * DT_MDL  # 20deg in 1 second is well within curvature limits
ROLL_MIN, ROLL_MAX = math.radians(-10), math.radians(10)
ROLL_LOWERED_MAX = math.radians(8)
ROLL_STD_MAX = math.radians(1.5)
LATERAL_ACC_SENSOR_THRESHOLD = 4.0
OFFSET_MAX = 10.0
OFFSET_LOWERED_MAX = 8.0
MIN_ACTIVE_SPEED = 1.0
LOW_ACTIVE_SPEED = 10.0


class VehicleParamsLearner:
  def __init__(self, CP, steer_ratio, stiffness_factor, angle_offset, P_initial=None):
    self.kf = CarKalman(GENERATED_DIR, steer_ratio, stiffness_factor, angle_offset, P_initial)

    self.kf.filter.set_global("mass", CP.mass)
    self.kf.filter.set_global("rotational_inertia", CP.rotationalInertia)
    self.kf.filter.set_global("center_to_front", CP.centerToFront)
    self.kf.filter.set_global("center_to_rear", CP.wheelbase - CP.centerToFront)
    self.kf.filter.set_global("stiffness_front", CP.tireStiffnessFront)
    self.kf.filter.set_global("stiffness_rear", CP.tireStiffnessRear)

    self.min_sr, self.max_sr = 0.5 * CP.steerRatio, 2.0 * CP.steerRatio

    self.calibrator = PoseCalibrator()

    self.active = False

    self.yaw_rate = 0.0
    self.roll = 0.0

    self.avg_angle_offset = np.degrees(angle_offset)
    self.angle_offset = self.avg_angle_offset
    self.avg_offset_valid = True
    self.total_offset_valid = True
    self.roll_valid = True

  def reset(self, t):
    self.kf.reset(t)

  def handle_log(self, t, which, msg):
    if which == 'livePose':
      device_pose = Pose.from_live_pose(msg)
      calibrated_pose = self.calibrator.build_calibrated_pose(device_pose)

      yaw_rate, yaw_rate_std = calibrated_pose.angular_velocity.z, calibrated_pose.angular_velocity.z_std
      yaw_rate_valid = msg.angularVelocityDevice.valid
      yaw_rate_valid = yaw_rate_valid and 0 < yaw_rate_std < 10  # rad/s
      yaw_rate_valid = yaw_rate_valid and abs(yaw_rate) < 1  # rad/s
      if not yaw_rate_valid:
        # This is done to bound the yaw rate estimate when localizer values are invalid or calibrating
        yaw_rate, yaw_rate_std = 0.0, np.radians(10.0)
      self.yaw_rate = yaw_rate

      localizer_roll, localizer_roll_std = device_pose.orientation.x, device_pose.orientation.x_std
      localizer_roll_std = np.radians(1) if np.isnan(localizer_roll_std) else localizer_roll_std
      roll_valid = (localizer_roll_std < ROLL_STD_MAX) and (ROLL_MIN < localizer_roll < ROLL_MAX) and msg.sensorsOK
      if roll_valid:
        roll = localizer_roll
        # Experimentally found multiplier of 2 to be best trade-off between stability and accuracy or similar?
        roll_std = 2 * localizer_roll_std
      else:
        # This is done to bound the road roll estimate when localizer values are invalid
        roll = 0.0
        roll_std = np.radians(10.0)
      roll = np.clip(roll, self.roll - ROLL_MAX_DELTA, self.roll + ROLL_MAX_DELTA)

      if self.active:
        if msg.posenetOK:
          self.kf.predict_and_observe(t,
                                      ObservationKind.ROAD_FRAME_YAW_RATE,
                                      np.array([[-yaw_rate]]),
                                      np.array([np.atleast_2d(yaw_rate_std**2)]))

          self.kf.predict_and_observe(t,
                                      ObservationKind.ROAD_ROLL,
                                      np.array([[roll]]),
                                      np.array([np.atleast_2d(roll_std**2)]))
        self.kf.predict_and_observe(t, ObservationKind.ANGLE_OFFSET_FAST, np.array([[0]]))

        # We observe the current stiffness and steer ratio (with a high observation noise) to bound
        # the respective estimate STD. Otherwise the STDs keep increasing, causing rapid changes in the
        # states in longer routes (especially straight stretches).
        stiffness = float(self.kf.x[States.STIFFNESS].item())
        steer_ratio = float(self.kf.x[States.STEER_RATIO].item())
        self.kf.predict_and_observe(t, ObservationKind.STIFFNESS, np.array([[stiffness]]))
        self.kf.predict_and_observe(t, ObservationKind.STEER_RATIO, np.array([[steer_ratio]]))

    elif which == 'liveCalibration':
      self.calibrator.feed_live_calib(msg)

    elif which == 'carState':
      steering_angle = msg.steeringAngleDeg
      speed = msg.vEgo

      in_linear_region = abs(steering_angle) < 45
      self.active = speed > MIN_ACTIVE_SPEED and in_linear_region
      self.moderate_speed = speed > LOW_ACTIVE_SPEED

      if self.active:
        self.kf.predict_and_observe(t, ObservationKind.STEER_ANGLE, np.array([[math.radians(msg.steeringAngleDeg)]]))
        self.kf.predict_and_observe(t, ObservationKind.ROAD_FRAME_X_SPEED, np.array([[speed]]))

    if not self.active:
      # Reset time when stopped so uncertainty doesn't grow
      self.kf.filter.set_filter_time(t)
      self.kf.filter.reset_rewind()

  def get_msg(self, valid, debug=False):
    x = self.kf.x
    P = np.sqrt(self.kf.P.diagonal())
    if not all(map(math.isfinite, x)):
      cloudlog.error("NaN in liveParameters estimate. Resetting to default values")
      self.reset(self.kf.t)
      x = self.kf.x

    self.avg_angle_offset = np.clip(math.degrees(x[States.ANGLE_OFFSET].item()),
                                self.avg_angle_offset - MAX_ANGLE_OFFSET_DELTA, self.avg_angle_offset + MAX_ANGLE_OFFSET_DELTA)
    self.angle_offset = np.clip(math.degrees(x[States.ANGLE_OFFSET].item() + x[States.ANGLE_OFFSET_FAST].item()),
                        self.angle_offset - MAX_ANGLE_OFFSET_DELTA, self.angle_offset + MAX_ANGLE_OFFSET_DELTA)
    self.roll = np.clip(float(x[States.ROAD_ROLL].item()), self.roll - ROLL_MAX_DELTA, self.roll + ROLL_MAX_DELTA)
    roll_std = float(P[States.ROAD_ROLL].item())
    if self.active and self.moderate_speed:
      # Account for the opposite signs of the yaw rates
      # At low speeds, bumping into a curb can cause the yaw rate to be very high
      sensors_valid = bool(abs(self.speed * (x[States.YAW_RATE].item() + self.yaw_rate)) < LATERAL_ACC_SENSOR_THRESHOLD)
    else:
      sensors_valid = True
    self.avg_offset_valid = check_valid_with_hysteresis(self.avg_offset_valid, self.avg_angle_offset, OFFSET_MAX, OFFSET_LOWERED_MAX)
    self.total_offset_valid = check_valid_with_hysteresis(self.total_offset_valid, self.angle_offset, OFFSET_MAX, OFFSET_LOWERED_MAX)
    self.roll_valid = check_valid_with_hysteresis(self.roll_valid, self.roll, ROLL_MAX, ROLL_LOWERED_MAX)

    msg = messaging.new_message('liveParameters')

    msg.valid = valid

    liveParameters = msg.liveParameters
    liveParameters.posenetValid = True
    liveParameters.sensorValid = sensors_valid
    liveParameters.steerRatio = float(x[States.STEER_RATIO].item())
    liveParameters.stiffnessFactor = float(x[States.STIFFNESS].item())
    liveParameters.roll = float(self.roll)
    liveParameters.angleOffsetAverageDeg = float(self.avg_angle_offset)
    liveParameters.angleOffsetDeg = float(self.angle_offset)
    liveParameters.steerRatioValid = self.min_sr <= liveParameters.steerRatio <= self.max_sr
    liveParameters.stiffnessFactorValid = 0.2 <= liveParameters.stiffnessFactor <= 5.0
    liveParameters.angleOffsetAverageValid = bool(self.avg_offset_valid)
    liveParameters.angleOffsetValid = bool(self.total_offset_valid)
    liveParameters.valid = all((
      liveParameters.angleOffsetAverageValid,
      liveParameters.angleOffsetValid ,
      self.roll_valid,
      roll_std < ROLL_STD_MAX,
      liveParameters.stiffnessFactorValid,
      liveParameters.steerRatioValid,
    ))
    liveParameters.steerRatioStd = float(P[States.STEER_RATIO].item())
    liveParameters.stiffnessFactorStd = float(P[States.STIFFNESS].item())
    liveParameters.angleOffsetAverageStd = float(P[States.ANGLE_OFFSET].item())
    liveParameters.angleOffsetFastStd = float(P[States.ANGLE_OFFSET_FAST].item())
    if debug:
      liveParameters.debugFilterState = log.LiveParametersData.FilterState.new_message()
      liveParameters.debugFilterState.value = x.tolist()
      liveParameters.debugFilterState.std = P.tolist()

    return msg


def check_valid_with_hysteresis(current_valid: bool, val: float, threshold: float, lowered_threshold: float):
  if current_valid:
    current_valid = abs(val) < threshold
  else:
    current_valid = abs(val) < lowered_threshold
  return current_valid


def retrieve_initial_vehicle_params(params_reader, CP, replay=False, debug=False):
  last_parameters_data = params_reader.get("LiveParameters")
  last_carparams_data = params_reader.get("CarParamsPrevRoute")

  steer_ratio, stiffness_factor, angle_offset_deg, p_initial = CP.steerRatio, 1.0, 0.0, None

  retrieve_success = True
  if last_parameters_data is not None and last_carparams_data is not None:
    try:
      with log.Event.from_bytes(last_parameters_data) as last_lp_msg, car.CarParams.from_bytes(last_carparams_data) as last_CP:
        lp = last_lp_msg.liveParameters
        # Check if car model matches
        if last_CP.carFingerprint != CP.carFingerprint:
          raise Exception(f"Car model mismatch")

        # Check if starting values are sane
        min_sr, max_sr = 0.5 * CP.steerRatio, 2.0 * CP.steerRatio
        steer_ratio_sane = min_sr <= lp.steerRatio <= max_sr
        if not steer_ratio_sane:
          raise Exception(f"Invalid starting values found {lp}")

        initial_filter_std = np.array(lp.debugFilterState.std)
        if debug and len(initial_filter_std) != 0:
          p_initial = initial_filter_std

        steer_ratio, stiffness_factor, angle_offset_deg = lp.steerRatio, lp.stiffnessFactor, lp.angleOffsetAverageDeg
    except Exception as e:
      cloudlog.error(f"Failed to retrieve initial values: {e}")
      retrieve_success = False

  if not retrieve_success:
    cloudlog.info("Parameter learner resetting to default values")

  return steer_ratio, stiffness_factor, angle_offset_deg, p_initial


def main():
  config_realtime_process([0, 1, 2, 3], 5)

  DEBUG = bool(int(os.getenv("DEBUG", "0")))
  REPLAY = bool(int(os.getenv("REPLAY", "0")))

  pm = messaging.PubMaster(['liveParameters'])
  sm = messaging.SubMaster(['livePose', 'liveCalibration', 'carState'], poll='livePose')

  params_reader = Params()
  # wait for stats about the car to come in from controls
  cloudlog.info("paramsd is waiting for CarParams")
  CP = messaging.log_from_bytes(params_reader.get("CarParams", block=True), car.CarParams)
  cloudlog.info("paramsd got CarParams")

  steer_ratio, stiffness_factor, angle_offset_deg, pInitial = retrieve_initial_vehicle_params(params_reader, CP, REPLAY, DEBUG)
  learner = VehicleParamsLearner(CP, steer_ratio, stiffness_factor, np.radians(angle_offset_deg), pInitial)

  while True:
    sm.update()
    if sm.all_checks():
      for which in sorted(sm.updated.keys(), key=lambda x: sm.logMonoTime[x]):
        if sm.updated[which]:
          t = sm.logMonoTime[which] * 1e-9
          learner.handle_log(t, which, sm[which])

    if sm.updated['livePose']:
      msg = learner.get_msg(sm.all_checks(), debug=DEBUG)

      msg_dat = msg.to_bytes()
      if sm.frame % 1200 == 0:  # once a minute
        params_reader.put_nonblocking("LiveParameters", msg_dat)

      pm.send('liveParameters', msg_dat)


if __name__ == "__main__":
  main()
