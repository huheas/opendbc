#!/usr/bin/env python3
import json
import os
from cereal import car
from math import fabs, exp
from openpilot.common.params import Params
from opendbc.car import get_safety_config, get_friction, structs
from opendbc.car.common.basedir import BASEDIR
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.gm.carcontroller import CarController
from opendbc.car.gm.carstate import CarState
from opendbc.car.gm.radar_interface import RadarInterface, RADAR_HEADER_MSG
from opendbc.car.gm.values import CAR, CarControllerParams, EV_CAR, CAMERA_ACC_CAR, CanBus, GMFlags, CC_ONLY_CAR, SDGM_CAR, CruiseButtons, GMSafetyFlags, ALT_ACCS
from opendbc.car.interfaces import CarInterfaceBase, TorqueFromLateralAccelCallbackType, FRICTION_THRESHOLD, LatControlInputs, NanoFFModel

#ButtonType = structs.CarState.ButtonEvent.Type 이 두 줄도 사용되지 않습니다.
#GearShifter = structs.CarState.GearShifter
TransmissionType = structs.CarParams.TransmissionType
NetworkLocation = structs.CarParams.NetworkLocation

CAM_MSG = 0x320  # AEBCmd
                 # TODO: Is this always linked to camera presence?
ACCELERATOR_POS_MSG = 0xbe

NON_LINEAR_TORQUE_PARAMS = {
  CAR.CHEVROLET_BOLT_EUV: [2.6531724862969748, 1.0, 0.1919764879840985, 0.009054123646805178],
  # CAR.CHEVROLET_BOLT_CC: [2.6531724862969748, 1.0, 0.1919764879840985, 0.009054123646805178],
  CAR.CHEVROLET_BOLT_CC: [1.8, 1.1, 0.3, -0.045],
  CAR.GMC_ACADIA: [4.78003305, 1.0, 0.3122, 0.05591772],
  CAR.CHEVROLET_SILVERADO: [3.29974374, 1.0, 0.25571356, 0.0465122]
}

NEURAL_PARAMS_PATH = os.path.join(BASEDIR, 'torque_data/neural_ff_weights.json')

PEDAL_MSG = 0x201

class CarInterface(CarInterfaceBase):
  CarState = CarState
  CarController = CarController
  RadarInterface = RadarInterface

  @staticmethod
  def get_pid_accel_limits(CP, current_speed, cruise_speed):
    return CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX

  # Determined by iteratively plotting and minimizing error for f(angle, speed) = steer.
  @staticmethod
  def get_steer_feedforward_volt(desired_angle, v_ego):
    desired_angle *= 0.02904609
    sigmoid = desired_angle / (1 + fabs(desired_angle))
    return 0.10006696 * sigmoid * (v_ego + 3.12485927)

  def get_steer_feedforward_function(self):
    if self.CP.carFingerprint in (CAR.CHEVROLET_VOLT, CAR.CHEVROLET_VOLT_CC):
      return self.get_steer_feedforward_volt
    else:
      return CarInterfaceBase.get_steer_feedforward_default

  def torque_from_lateral_accel_siglin(self, latcontrol_inputs: LatControlInputs, torque_params: structs.CarParams.LateralTorqueTuning,
                                       lateral_accel_error: float, lateral_accel_deadzone: float, friction_compensation: bool, gravity_adjusted: bool) -> float:
    friction = get_friction(lateral_accel_error, lateral_accel_deadzone, FRICTION_THRESHOLD, torque_params, friction_compensation)

    def sig(val):
      # https://timvieira.github.io/blog/post/2014/02/11/exp-normalize-trick
      if val >= 0:
        return 1 / (1 + exp(-val)) - 0.5
      else:
        z = exp(val)
        return z / (1 + z) - 0.5

    # The "lat_accel vs torque" relationship is assumed to be the sum of "sigmoid + linear" curves
    # An important thing to consider is that the slope at 0 should be > 0 (ideally >1)
    # This has big effect on the stability about 0 (noise when going straight)
    # ToDo: To generalize to other GMs, explore tanh function as the nonlinear
    non_linear_torque_params = NON_LINEAR_TORQUE_PARAMS.get(self.CP.carFingerprint)
    assert non_linear_torque_params, "The params are not defined"
    a, b, c, _ = non_linear_torque_params
    steer_torque = (sig(latcontrol_inputs.lateral_acceleration * a) * b) + (latcontrol_inputs.lateral_acceleration * c)
    return float(steer_torque) + friction

  def torque_from_lateral_accel_neural(self, latcontrol_inputs: LatControlInputs, torque_params: structs.CarParams.LateralTorqueTuning,
                                       lateral_accel_error: float, lateral_accel_deadzone: float, friction_compensation: bool, gravity_adjusted: bool) -> float:
    friction = get_friction(lateral_accel_error, lateral_accel_deadzone, FRICTION_THRESHOLD, torque_params, friction_compensation)
    inputs = list(latcontrol_inputs)
    if gravity_adjusted:
      inputs[0] += inputs[1]
    return float(self.neural_ff_model.predict(inputs)) + friction

  def torque_from_lateral_accel(self) -> TorqueFromLateralAccelCallbackType:
    with open(NEURAL_PARAMS_PATH) as f:
      neural_ff_cars = json.load(f).keys()
    if self.CP.carFingerprint in neural_ff_cars:
      self.neural_ff_model = NanoFFModel(NEURAL_PARAMS_PATH, self.CP.carFingerprint)
      return self.torque_from_lateral_accel_neural
    elif self.CP.carFingerprint in NON_LINEAR_TORQUE_PARAMS:
      return self.torque_from_lateral_accel_siglin
    else:
      return self.torque_from_lateral_accel_linear

  @staticmethod
  def _get_params(ret: structs.CarParams, candidate, fingerprint, car_fw, alpha_long, docs) -> structs.CarParams:
    ret.brand = "gm"
    ret.safetyConfigs = [get_safety_config(structs.CarParams.SafetyModel.gm)]
    ret.autoResumeSng = False
    ret.enableBsm = 0x142 in fingerprint[CanBus.POWERTRAIN] or 0x142 in fingerprint[CanBus.CAMERA]
    ret.startAccel = 1.0

    useEVTables = Params().get_bool("EVTable")

    if PEDAL_MSG in fingerprint[0]:
      ret.enableGasInterceptorDEPRECATED = True
      ret.safetyConfigs[0].safetyParam |= GMSafetyFlags.GAS_INTERCEPTOR.value

    if candidate in EV_CAR:
      ret.transmissionType = TransmissionType.direct
    else:
      ret.transmissionType = TransmissionType.automatic

    ret.longitudinalTuning.kpBP = [0.]
    ret.longitudinalTuning.kiBP = [0.]

    if candidate in (CAMERA_ACC_CAR | SDGM_CAR):
      ret.alphaLongitudinalAvailable = candidate not in (CC_ONLY_CAR | SDGM_CAR)
      ret.networkLocation = NetworkLocation.fwdCamera
      ret.radarUnavailable = True  # no radar
      ret.pcmCruise = True
      ret.safetyConfigs[0].safetyParam |= GMSafetyFlags.HW_CAM.value
      ret.minEnableSpeed = -1 * CV.KPH_TO_MS
      ret.minSteerSpeed = 10 * CV.KPH_TO_MS

      # Tuning for experimental long
      ret.longitudinalTuning.kpV = [1.0]
      ret.longitudinalTuning.kiV = [1.0]
      ret.stoppingDecelRate = 2.0  # reach brake quickly after enabling
      ret.vEgoStopping = 0.25
      ret.vEgoStarting = 0.25
      ret.stopAccel = -0.4
      ret.startingState = True
      ret.startAccel = 1.5

      if alpha_long:
        ret.pcmCruise = False
        ret.openpilotLongitudinalControl = True
        ret.safetyConfigs[0].safetyParam |= GMSafetyFlags.HW_CAM_LONG.value

      if candidate in ALT_ACCS:
        ret.alphaLongitudinalAvailable = False
        ret.openpilotLongitudinalControl = False
        ret.minEnableSpeed = -1.  # engage speed is decided by PCM

    else:  # ASCM, OBD-II harness
      ret.openpilotLongitudinalControl = True
      ret.networkLocation = NetworkLocation.gateway
      ret.radarUnavailable = False # kans
      ret.pcmCruise = False  # stock non-adaptive cruise control is kept off
      # supports stop and go, but initial engage must (conservatively) be above 18mph
      ret.minEnableSpeed = -1 * CV.MPH_TO_MS
      ret.minSteerSpeed = (6.7 if useEVTables else 7) * CV.MPH_TO_MS

      # Tuning
      ret.longitudinalTuning.kpV = [1.0]
      ret.longitudinalTuning.kiV = [0.3]

      # TODO: Test for CADILLAC_CT6_ACC
      if ret.enableGasInterceptorDEPRECATED:
        # Need to set ASCM long limits when using pedal interceptor, instead of camera ACC long limits
        ret.safetyConfigs[0].safetyParam |= GMSafetyFlags.HW_ASCM_LONG.value

    # These cars have been put into dashcam only due to both a lack of users and test coverage.
    # These cars likely still work fine. Once a user confirms each car works and a test route is
    # added to opendbc/car/tests/routes.py, we can remove it from this list.
    # ret.dashcamOnly = candidate in {CAR.CADILLAC_ATS, CAR.HOLDEN_ASTRA, CAR.CHEVROLET_MALIBU, CAR.BUICK_REGAL} or \
    #                   (ret.networkLocation == NetworkLocation.gateway and ret.radarUnavailable)

    # Start with a baseline tuning for all GM vehicles. Override tuning as needed in each model section below.
    ret.steerActuatorDelay = 0.28  # Default delay, not measured yet

    ret.steerLimitTimer = 0.4
    ret.longitudinalActuatorDelay = Params().get_float("LongActuatorDelay")*0.01 # 0.5  # large delay to initially start braking

    if candidate == CAR.CHEVROLET_VOLT:
      ret.steerActuatorDelay = 0.45 if useEVTables else 0.3
      ret.longitudinalTuning.kpBP = [0.]
      ret.longitudinalTuning.kpV = [1.0]
      ret.longitudinalTuning.kiBP = [0.]
      ret.longitudinalTuning.kiV = [.35]
      ret.longitudinalTuning.kf = 1.0
      ret.stoppingDecelRate = 0.2 # brake_travel/s while trying to stop
      ret.vEgoStopping = 0.1
      ret.vEgoStarting = 0.05
      ret.stopAccel = -0.5
      ret.startingState = True
      ret.startAccel = 1.9

      # softer long tune for ev table
      if useEVTables:
        ret.longitudinalTuning.kpBP = [0.]
        ret.longitudinalTuning.kpV = [1.0]
        ret.longitudinalTuning.kiBP = [0.]
        ret.longitudinalTuning.kiV = [.35]
        ret.longitudinalTuning.kf = 1.0
        ret.stoppingDecelRate = 1.0 # brake_travel/s while trying to stop
        ret.stopAccel = -0.5
        ret.startAccel = 0.6

      useTorque = Params().get_bool("LateralTorqueCustom")
      if useTorque:
        CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)
      else:
        ret.lateralTuning.pid.kpBP = [0., 40.]
        ret.lateralTuning.pid.kpV = [0., 0.17]
        ret.lateralTuning.pid.kiBP = [0.]
        ret.lateralTuning.pid.kiV = [0.]
        ret.lateralTuning.pid.kf = 1.

    elif candidate == CAR.CADILLAC_CT6_ACC:
      ret.steerActuatorDelay = 0.3
      ret.longitudinalTuning.kpBP = [0.]
      ret.longitudinalTuning.kpV = [1.0]
      ret.longitudinalTuning.kiBP = [0.]
      ret.longitudinalTuning.kiV = [.3]
      ret.longitudinalTuning.kf = 1.0
      ret.stoppingDecelRate = 0.2 # brake_travel/s while trying to stop
      ret.stopAccel = -0.5
      ret.startingState = True
      ret.startAccel = 1.5

      useTorque = Params().get_bool("LateralTorqueCustom")
      if useTorque:
        CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)
      else:
        ret.lateralTuning.pid.kpBP = [0., 40.]
        ret.lateralTuning.pid.kpV = [0., 0.17]
        ret.lateralTuning.pid.kiBP = [0.]
        ret.lateralTuning.pid.kiV = [0.]
        ret.lateralTuning.pid.kf = 1.

    elif candidate == CAR.GMC_ACADIA:
      ret.minEnableSpeed = -1.  # engage speed is decided by pcm
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate in (CAR.CHEVROLET_MALIBU, CAR.CHEVROLET_MALIBU_CC):
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.BUICK_LACROSSE:
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.CADILLAC_ESCALADE:
      ret.minEnableSpeed = -1.  # engage speed is decided by pcm
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate in (CAR.CADILLAC_ESCALADE_ESV, CAR.CADILLAC_ESCALADE_ESV_2019):
      ret.minEnableSpeed = -1.  # engage speed is decided by pcm

      if candidate == CAR.CADILLAC_ESCALADE_ESV:
        ret.lateralTuning.pid.kiBP, ret.lateralTuning.pid.kpBP = [[10., 41.0], [10., 41.0]]
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.13, 0.24], [0.01, 0.02]]
        ret.lateralTuning.pid.kf = 0.000045
      else:
        ret.steerActuatorDelay = 0.2
        CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate in (CAR.CHEVROLET_BOLT_EUV, CAR.CHEVROLET_BOLT_CC):
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

      if ret.enableGasInterceptorDEPRECATED:
        # ACC Bolts use pedal for full longitudinal control, not just sng
        ret.flags |= GMFlags.PEDAL_LONG.value

    elif candidate == CAR.CHEVROLET_SILVERADO:
      # On the Bolt, the ECM and camera independently check that you are either above 5 kph or at a stop
      # with foot on brake to allow engagement, but this platform only has that check in the camera.
      # TODO: check if this is split by EV/ICE with more platforms in the future
      if ret.openpilotLongitudinalControl:
        ret.minEnableSpeed = -1.
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate in (CAR.CHEVROLET_EQUINOX, CAR.CHEVROLET_EQUINOX_CC):
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate in (CAR.CHEVROLET_TRAILBLAZER, CAR.CHEVROLET_TRAILBLAZER_CC):
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate in (CAR.CHEVROLET_SUBURBAN, CAR.CHEVROLET_SUBURBAN_CC):
      ret.steerActuatorDelay = 0.075
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.GMC_YUKON_CC:
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.CADILLAC_XT4:
      ret.steerActuatorDelay = 0.2
      ret.minEnableSpeed = -1.  # engage speed is decided by pcm
      ret.minSteerSpeed = 30 * CV.MPH_TO_MS
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)
    elif candidate == CAR.CHEVROLET_VOLT_2019:
      ret.steerActuatorDelay = 0.2
      ret.minEnableSpeed = -1.  # engage speed is decided by pcm
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.CADILLAC_XT5_CC:
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.CHEVROLET_TRAVERSE:
      ret.steerActuatorDelay = 0.2
      ret.minEnableSpeed = -1.  # engage speed is decided by pcm
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.BUICK_BABYENCLAVE:
      ret.steerActuatorDelay = 0.2
      ret.minEnableSpeed = -1.  # engage speed is decided by pcm
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.CADILLAC_CT6_CC:
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.CHEVROLET_MALIBU_CC:
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.CHEVROLET_TRAX:
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)
      ret.stoppingDecelRate = 0.3
      ret.minEnableSpeed = -1.
      ret.stopAccel = -0.5
      ret.startingState = True
      ret.startAccel = 1.5
    elif candidate == CAR.CHEVROLET_TRAVERSE:
      ret.steerActuatorDelay = 0.2
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)

    elif candidate == CAR.GMC_YUKON:
      ret.steerActuatorDelay = 0.5
      CarInterfaceBase.configure_torque_tune(candidate, ret.lateralTuning)
      ret.dashcamOnly = True  # Needs steerRatio, tireStiffness, and lat accel factor tuning

    if ret.enableGasInterceptorDEPRECATED:
      ret.networkLocation = NetworkLocation.fwdCamera
      ret.safetyConfigs[0].safetyParam |= GMSafetyFlags.HW_CAM.value
      ret.minEnableSpeed = -1
      ret.pcmCruise = False
      ret.openpilotLongitudinalControl = True
      ret.autoResumeSng = True

      if candidate in CC_ONLY_CAR:
        ret.flags |= GMFlags.PEDAL_LONG.value
        ret.safetyConfigs[0].safetyParam |= GMSafetyFlags.PEDAL_LONG.value
        # Note: Low speed, stop and go not tested. Should be fairly smooth on highway
        ret.longitudinalTuning.kpBP = [0., 3., 6., 35.]
        ret.longitudinalTuning.kpV = [0.08, 0.175, 0.225, 0.33]
        ret.longitudinalTuning.kiBP = [0., 35.0]
        ret.longitudinalTuning.kiV = [0.07, 0.07]
        ret.longitudinalTuning.kf = 0.25
        ret.stoppingDecelRate = 0.8
      else:  # Pedal used for SNG, ACC for longitudinal control otherwise
        ret.safetyConfigs[0].safetyParam |= GMSafetyFlags.HW_CAM_LONG.value
        ret.startingState = True
        ret.vEgoStopping = 0.25
        ret.vEgoStarting = 0.25

    elif candidate in CC_ONLY_CAR:
      ret.flags |= GMFlags.CC_LONG.value
      ret.safetyConfigs[0].safetyParam |= GMSafetyFlags.CC_LONG.value
      if alpha_long:
        ret.openpilotLongitudinalControl = True
        ret.flags |= GMFlags.CC_LONG.value
      ret.radarUnavailable = True
      ret.alphaLongitudinalAvailable = True
      ret.minEnableSpeed = 24 * CV.MPH_TO_MS
      ret.pcmCruise = True

      ret.stoppingDecelRate = 11.18  # == 25 mph/s (.04 rate)

      ret.longitudinalTuning.kiBP = [10.7, 10.8, 28.]
      ret.longitudinalTuning.kiV = [0., 20., 20.]  # set lower end to 0 since we can't drive below that speed

    if candidate in CC_ONLY_CAR:
      ret.safetyConfigs[0].safetyParam |= GMSafetyFlags.NO_ACC.value

    # Exception for flashed cars, or cars whose camera was removed
    if (ret.networkLocation == NetworkLocation.fwdCamera or candidate in CC_ONLY_CAR) and CAM_MSG not in fingerprint[
      CanBus.CAMERA] and not candidate in SDGM_CAR:
      ret.flags |= GMFlags.NO_CAMERA.value
      ret.safetyConfigs[0].safetyParam |= GMSafetyFlags.NO_CAMERA.value

    if ACCELERATOR_POS_MSG not in fingerprint[CanBus.POWERTRAIN]:
      ret.flags |= GMFlags.NO_ACCELERATOR_POS_MSG.value

    if 608 in fingerprint[CanBus.POWERTRAIN]:
      ret.flags |= GMFlags.SPEED_RELATED_MSG.value


    return ret
