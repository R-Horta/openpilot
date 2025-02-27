import copy
from cereal import car
from common.numpy_fast import mean
from common.filter_simple import FirstOrderFilter
from common.realtime import DT_CTRL
from opendbc.can.can_define import CANDefine
from selfdrive.car.interfaces import CarStateBase
from opendbc.can.parser import CANParser
from selfdrive.config import Conversions as CV
from selfdrive.car.toyota.values import CAR, DBC, STEER_THRESHOLD, NO_STOP_TIMER_CAR, TSS2_CAR, FEATURES
from selfdrive.car.toyota.values import CAR, DBC, STEER_THRESHOLD, NO_STOP_TIMER_CAR, TSS2_CAR
from selfdrive.swaglog import cloudlog
from common.realtime import DT_CTRL, sec_since_boot
from common.params import Params


class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    can_define = CANDefine(DBC[CP.carFingerprint]["pt"])
    self.shifter_values = can_define.dv["GEAR_PACKET"]["GEAR"]

    # All TSS2 car have the accurate sensor
    self.accurate_steer_angle_seen = CP.carFingerprint in TSS2_CAR

    # On cars with cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]
    # the signal is zeroed to where the steering angle is at start.
    # Need to apply an offset as soon as the steering angle measurements are both received
    self.needs_angle_offset_torque = CP.carFingerprint not in TSS2_CAR #offset only if needed
    self.needs_angle_offset_zss = True #ZSS always needs offset
    self.angle_offset_zss = 0.
    self.angle_offset_torque = 0.
    self.stock_steer_value = 0.
    self.zorro_steer_value = 0.
    self.torque_steer_value = 0.
    self.cruise_active_previous = False
    self.cruise_active = False
    self.out_of_tolerance_counter = 0
    self.steertype = 0 #for debug purposes. 0-undefined, 1-stock, 2-torque, 3-zorro
    self.count = 0

    self.low_speed_lockout = False
    self.acc_type = 1
    self.steer_not_allowed = False
    self.resumeAvailable = False
    self.accEnabled = False
    self.lkasEnabled = False
    self.leftBlinkerOn = False
    self.rightBlinkerOn = False
    self.disengageByBrake = False
    self.belowLaneChangeSpeed = True
    self.automaticLaneChange = True #TODO: add setting back

    self.cruise_buttons = 0
    self.prev_cruise_buttons = 0

    self.lkas_enabled = None
    self.prev_lkas_enabled = None

    self.acc_mads_combo = None
    self.prev_acc_mads_combo = None

    self.cruiseState_standstill = False

    # KRKeegan - Add support for toyota distance button
    self.gap_adjust_cruise_tr = 0

    # AleSato's automatic brakehold
    self.time_to_brakehold = 100 * 3   # 3 seconds stopped to activate
    self.GearShifter = car.CarState.GearShifter # avoid Rear and Park gears
    self.stock_aeb = {}
    self.brakehold_condition_satisfied = False
    self.brakehold_condition_counter = 0
    self.reset_brakehold = False    
    self.prev_brakePressed = True
    self.brakehold_governor = False


  def update(self, cp, cp_cam):
    ret = car.CarState.new_message()

    # update prevs, update must run once per loop
    self.prev_cruise_buttons = self.cruise_buttons
    self.prev_lkas_enabled = self.lkas_enabled
    self.mads_enabled = Params().get_bool("EnableMADS")
    self.acc_mads_combo = Params().get_bool("ACCMADSCombo")
    self.gap_adjust_cruise = Params().get_bool("GapAdjustCruise")
    self.gap_adjust_cruise_tr_line = int(Params().get("GapAdjustCruiseTr"))

    ret.doorOpen = any([cp.vl["SEATS_DOORS"]["DOOR_OPEN_FL"], cp.vl["SEATS_DOORS"]["DOOR_OPEN_FR"],
                        cp.vl["SEATS_DOORS"]["DOOR_OPEN_RL"], cp.vl["SEATS_DOORS"]["DOOR_OPEN_RR"]])
    ret.seatbeltUnlatched = cp.vl["SEATS_DOORS"]["SEATBELT_DRIVER_UNLATCHED"] != 0

    ret.brakePressed = cp.vl["BRAKE_MODULE"]["BRAKE_PRESSED"] != 0
    ret.brakeHoldActive = cp.vl["ESP_CONTROL"]["BRAKE_HOLD_ACTIVE"] == 1
    ret.brakeLights = bool(cp.vl["ESP_CONTROL"]["BRAKE_LIGHTS_ACC"] or ret.brakePressed or ret.brakeHoldActive)
    if self.CP.enableGasInterceptor:
      ret.gas = (cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS"] + cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS2"]) / 2.
      ret.gasPressed = ret.gas > 15
    else:
      ret.gas = cp.vl["GAS_PEDAL"]["GAS_PEDAL"]
      ret.gasPressed = cp.vl["PCM_CRUISE"]["GAS_RELEASED"] == 0

    ret.wheelSpeeds = self.get_wheel_speeds(
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_FL"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_FR"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_RL"],
      cp.vl["WHEEL_SPEEDS"]["WHEEL_SPEED_RR"],
    )
    ret.vEgoRaw = mean([ret.wheelSpeeds.fl, ret.wheelSpeeds.fr, ret.wheelSpeeds.rl, ret.wheelSpeeds.rr])
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)

    self.belowLaneChangeSpeed = ret.vEgo < (30 * CV.MPH_TO_MS)

    ret.standstill = ret.vEgoRaw < 0.001
    ret.standStill = self.CP.standStill

    self.cruiseState_standstill = ret.cruiseState.standstill

    self.cruise_active = bool(cp.vl["PCM_CRUISE"]["CRUISE_ACTIVE"])
    self.stock_steer_value = cp.vl["STEER_ANGLE_SENSOR"]["STEER_ANGLE"] + cp.vl["STEER_ANGLE_SENSOR"]["STEER_FRACTION"]

    # Some newer models have a more accurate angle measurement in the TORQUE_SENSOR message. Use if non-zero
    if abs(cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]) > 1e-3:
      self.accurate_steer_angle_seen = True

    if self.cruise_active and not self.cruise_active_previous:
      self.needs_angle_offset_zss = True # cruise was just activated, so allow offset to be recomputed
      self.out_of_tolerance_counter = 0 # Allow ZSS re-use after disengage and re-engage
    self.cruise_active_previous = self.cruise_active

    #compute offset for torque steer
    if self.accurate_steer_angle_seen:
      if self.needs_angle_offset_torque:
        angle_wheel = cp.vl["STEER_ANGLE_SENSOR"]["STEER_ANGLE"] + cp.vl["STEER_ANGLE_SENSOR"]["STEER_FRACTION"]
        if (abs(angle_wheel) > 1e-3 and abs(cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"]) > 1e-3):
          self.needs_angle_offset_torque = False
          self.angle_offset_torque = cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"] - angle_wheel
      self.torque_steer_value = cp.vl["STEER_TORQUE_SENSOR"]["STEER_ANGLE"] - self.angle_offset_torque

    if self.CP.hasZss:
      #compute offset for zorro steer
      if self.needs_angle_offset_zss:
        angle_wheel = cp.vl["STEER_ANGLE_SENSOR"]["STEER_ANGLE"] + cp.vl["STEER_ANGLE_SENSOR"]["STEER_FRACTION"]
        if (abs(angle_wheel) > 1e-3 and abs(cp.vl["SECONDARY_STEER_ANGLE"]["ZORRO_STEER"]) > 1e-3):
          self.needs_angle_offset_zss = False
          self.angle_offset_zss = cp.vl["SECONDARY_STEER_ANGLE"]["ZORRO_STEER"] - angle_wheel
      self.zorro_steer_value = cp.vl["SECONDARY_STEER_ANGLE"]["ZORRO_STEER"] - self.angle_offset_zss

      #default to stock if 1) too many instances of steering being out of tolerance; something is not right
      #                    2) zorro steer offset has not been computed
      if self.out_of_tolerance_counter < 10 and not self.needs_angle_offset_zss:
        #check if zorro steer is out of tolerance
        if abs(self.stock_steer_value - self.zorro_steer_value) > 4.0:
          ret.steeringAngleDeg = self.stock_steer_value
          self.steertype = 1
          if self.cruise_active:
            self.out_of_tolerance_counter = self.out_of_tolerance_counter + 1 #should not get here too often with cruise active
        else:
          ret.steeringAngleDeg= self.zorro_steer_value
          self.steertype = 3
      else:
        ret.steeringAngleDeg = self.stock_steer_value
        self.steertype = 1
    elif self.accurate_steer_angle_seen:
      ret.steeringAngleDeg = self.torque_steer_value
      self.steertype = 2
    else:
      ret.steeringAngleDeg = self.stock_steer_value
      self.steertype = 1

    if (self.count % int(1.0 / DT_CTRL)) == 0:
      cloudlog.info("*** Zorro       *** %s" % self.zorro_steer_value)
      cloudlog.info("*** Torque      *** %s" % self.torque_steer_value)
      cloudlog.info("*** Stock       *** %s" % self.stock_steer_value)
      cloudlog.info("*** Num of OTs  *** %s" % self.out_of_tolerance_counter)
      cloudlog.info("*** OP is Using *** %s" % ret.steeringAngleDeg)
      steertypeText = "Undefined" #should never happen
      if self.steertype == 1:
        steertypeText = "Stock"
      elif self.steertype == 2:
        steertypeText = "Torque"
      elif self.steertype == 3:
        steertypeText = "Zorro"
      cloudlog.info("====================================")
      cloudlog.info("******* Using Steer Type: ******* %s" % steertypeText)
      cloudlog.info("====================================")

    ret.steeringRateDeg = cp.vl["STEER_ANGLE_SENSOR"]["STEER_RATE"]

    self.cruise_buttons = cp.vl["PCM_CRUISE"]["CRUISE_STATE"]
    if self.CP.carFingerprint in FEATURES["use_lta_msg"]:
      self.lkas_enabled = cp_cam.vl["LKAS_HUD"]["LDA_ON_MESSAGE"]
      self.persistLkasIconDisabled = cp_cam.vl["LKAS_HUD"]["SET_ME_X01"] == 1
    else:
      self.lkas_enabled = cp_cam.vl["LKAS_HUD"]["SET_ME_X01"]
      self.persistLkasIconDisabled = cp_cam.vl["LKAS_HUD"]["SET_ME_X01"] == 0

    if self.prev_lkas_enabled is None:
      self.prev_lkas_enabled = self.lkas_enabled

    can_gear = int(cp.vl["GEAR_PACKET"]["GEAR"])
    ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(can_gear, None))
    ret.leftBlinker = cp.vl["STEERING_LEVERS"]["TURN_SIGNALS"] == 1
    ret.rightBlinker = cp.vl["STEERING_LEVERS"]["TURN_SIGNALS"] == 2

    self.leftBlinkerOn = cp.vl["STEERING_LEVERS"]["TURN_SIGNALS"] == 1
    self.rightBlinkerOn = cp.vl["STEERING_LEVERS"]["TURN_SIGNALS"] == 2

    ret.steeringTorque = cp.vl["STEER_TORQUE_SENSOR"]["STEER_TORQUE_DRIVER"]
    ret.steeringTorqueEps = cp.vl["STEER_TORQUE_SENSOR"]["STEER_TORQUE_EPS"]
    # we could use the override bit from dbc, but it's triggered at too high torque values
    ret.steeringPressed = abs(ret.steeringTorque) > STEER_THRESHOLD

    if self.CP.carFingerprint in [CAR.LEXUS_IS, CAR.LEXUS_RC]:
      ret.cruiseState.available = cp.vl["DSU_CRUISE"]["MAIN_ON"] != 0
      ret.cruiseState.speed = cp.vl["DSU_CRUISE"]["SET_SPEED"] * CV.KPH_TO_MS
    else:
      ret.cruiseState.available = cp.vl["PCM_CRUISE_2"]["MAIN_ON"] != 0
      ret.cruiseState.speed = cp.vl["PCM_CRUISE_2"]["SET_SPEED"] * self.CP.wheelSpeedFactor * CV.KPH_TO_MS

    if self.CP.carFingerprint in TSS2_CAR:
      self.acc_type = 1 if Params().get_bool('StopAndGoHack') else cp_cam.vl["ACC_CONTROL"]["ACC_TYPE"]

    if self.CP.carFingerprint in TSS2_CAR:
      if self.gap_adjust_cruise:
        ret.gapAdjustCruiseTr = self.gap_adjust_cruise_tr_line
      else:
        if self.CP.carFingerprint in TSS2_CAR:
          # KRKeegan - Add support for toyota distance button
          self.gap_adjust_cruise_tr = 1 if cp_cam.vl["ACC_CONTROL"]["DISTANCE"] == 1 else 0
        elif self.CP.smartDsu:
          self.gap_adjust_cruise_tr = 1 if cp.vl["SDSU"]["FD_BUTTON"] == 1 else 0    
        ret.gapAdjustCruiseTr = cp.vl["PCM_CRUISE_SM"]["DISTANCE_LINES"]

    # Toyota 5/5 Speed Increments
    self.Fast_Speed_Increments = 2 if Params().get_bool('Change5speed') else 1

    # some TSS2 cars have low speed lockout permanently set, so ignore on those cars
    # these cars are identified by an ACC_TYPE value of 2.
    # TODO: it is possible to avoid the lockout and gain stop and go if you
    # send your own ACC_CONTROL msg on startup with ACC_TYPE set to 1
    if (self.CP.carFingerprint not in TSS2_CAR and self.CP.carFingerprint not in [CAR.LEXUS_IS, CAR.LEXUS_RC]) or \
       (self.CP.carFingerprint in TSS2_CAR and self.acc_type == 1):
      self.low_speed_lockout = cp.vl["PCM_CRUISE_2"]["LOW_SPEED_LOCKOUT"] == 2

    self.pcm_acc_status = cp.vl["PCM_CRUISE"]["CRUISE_STATE"]
    if self.CP.carFingerprint in NO_STOP_TIMER_CAR or self.CP.enableGasInterceptor:
      # ignore standstill in hybrid vehicles, since pcm allows to restart without
      # receiving any special command. Also if interceptor is detected
      ret.cruiseState.standstill = False
    else:
      ret.cruiseState.standstill = self.pcm_acc_status == 7
    ret.cruiseState.enabled = self.cruise_active
    ret.cruiseState.nonAdaptive = cp.vl["PCM_CRUISE"]["CRUISE_STATE"] in [1, 2, 3, 4, 5, 6]

    if ret.cruiseState.available:
      if self.mads_enabled:
        if self.CP.carFingerprint in FEATURES["use_lta_msg"]:
          if self.prev_lkas_enabled != 1 and self.lkas_enabled == 1: #1 == not LDA_ON_MESSAGE
            self.lkasEnabled = True
          elif self.prev_lkas_enabled != 2 and self.lkas_enabled == 2:
            self.lkasEnabled = False
        else:
          if not self.prev_lkas_enabled and self.lkas_enabled: #1 == not LKAS button
            self.lkasEnabled = True
          elif self.prev_lkas_enabled == 1 and not self.lkas_enabled:
            self.lkasEnabled = False
        if self.acc_mads_combo:
          if not self.prev_acc_mads_combo and ret.cruiseState.enabled:
            self.lkasEnabled = True
          self.prev_acc_mads_combo = ret.cruiseState.enabled
    else:
      self.lkasEnabled = False

    if self.prev_cruise_buttons != 0: # CANCEL
      if self.cruise_buttons == 0:
        if not self.mads_enabled:
          self.lkasEnabled = False
    if ret.brakePressed:
      if not self.mads_enabled:
        self.lkasEnabled = False

    if ret.cruiseState.enabled:
      if not self.mads_enabled:
        self.lkasEnabled = True

    if ret.cruiseState.enabled == True:
      self.resumeAvailable = True

    ret.steerWarning = False

    if self.lkasEnabled:
      # 2 is standby, 10 is active. TODO: check that everything else is really a faulty state
      steer_state = cp.vl["EPS_STATUS"]["LKA_STATE"]
      self.steer_not_allowed = steer_state in [9, 25]
      if (self.automaticLaneChange and not self.belowLaneChangeSpeed and (self.rightBlinkerOn or self.leftBlinkerOn)) or not (self.rightBlinkerOn or self.leftBlinkerOn):
        ret.steerWarning = cp.vl["EPS_STATUS"]["LKA_STATE"] not in [1, 5]


    ret.genericToggle = bool(cp.vl["LIGHT_STALK"]["AUTO_HIGH_BEAM"])
    ret.stockAeb = bool(cp_cam.vl["PRE_COLLISION"]["PRECOLLISION_ACTIVE"] and cp_cam.vl["PRE_COLLISION"]["FORCE"] < -1e-5)

    ret.espDisabled = cp.vl["ESP_CONTROL"]["TC_DISABLED"] != 0

    if self.CP.enableBsm:
      ret.leftBlindspot = (cp.vl["BSM"]["L_ADJACENT"] == 1) or (cp.vl["BSM"]["L_APPROACHING"] == 1)
      ret.rightBlindspot = (cp.vl["BSM"]["R_ADJACENT"] == 1) or (cp.vl["BSM"]["R_APPROACHING"] == 1)

    self.count = self.count + 1

    # AleSato's Automatic BrakeHold
    self.stock_aeb = copy.copy(cp_cam.vl["PRE_COLLISION_2"])
    self.brakehold_condition_satisfied =  (ret.standstill and ret.cruiseState.available and not ret.gasPressed and \
                                          not ret.cruiseState.enabled and not (ret.gearShifter in (self.GearShifter.reverse,\
                                          self.GearShifter.park)) and Params().get_bool('AleSato_AutomaticBrakeHold'))
    if self.brakehold_condition_satisfied:
      if self.brakehold_condition_counter > self.time_to_brakehold and not self.reset_brakehold:
        self.brakehold_governor = True
      else:
        self.brakehold_governor = False
      if not self.prev_brakePressed and ret.brakePressed: # disable automatic brakehold in second brakePress
        self.reset_brakehold = True
      self.brakehold_condition_counter += 1
    else:
      self.brakehold_governor = False
      self.reset_brakehold = False
      self.brakehold_condition_counter = 0  
    self.prev_brakePressed = ret.brakePressed

    return ret

  @staticmethod
  def get_can_parser(CP):

    signals = [
      # sig_name, sig_address, default
      ("STEER_ANGLE", "STEER_ANGLE_SENSOR", 0),
      ("GEAR", "GEAR_PACKET", 0),
      ("BRAKE_PRESSED", "BRAKE_MODULE", 0),
      ("GAS_PEDAL", "GAS_PEDAL", 0),
      ("WHEEL_SPEED_FL", "WHEEL_SPEEDS", 0),
      ("WHEEL_SPEED_FR", "WHEEL_SPEEDS", 0),
      ("WHEEL_SPEED_RL", "WHEEL_SPEEDS", 0),
      ("WHEEL_SPEED_RR", "WHEEL_SPEEDS", 0),
      ("DOOR_OPEN_FL", "SEATS_DOORS", 1),
      ("DOOR_OPEN_FR", "SEATS_DOORS", 1),
      ("DOOR_OPEN_RL", "SEATS_DOORS", 1),
      ("DOOR_OPEN_RR", "SEATS_DOORS", 1),
      ("SEATBELT_DRIVER_UNLATCHED", "SEATS_DOORS", 1),
      ("TC_DISABLED", "ESP_CONTROL", 1),
      ("BRAKE_HOLD_ACTIVE", "ESP_CONTROL", 1),
      ("BRAKE_LIGHTS_ACC", "ESP_CONTROL", 1),
      ("STEER_FRACTION", "STEER_ANGLE_SENSOR", 0),
      ("STEER_RATE", "STEER_ANGLE_SENSOR", 0),
      ("CRUISE_ACTIVE", "PCM_CRUISE", 0),
      ("CRUISE_STATE", "PCM_CRUISE", 0),
      ("GAS_RELEASED", "PCM_CRUISE", 1),
      ("STEER_TORQUE_DRIVER", "STEER_TORQUE_SENSOR", 0),
      ("STEER_TORQUE_EPS", "STEER_TORQUE_SENSOR", 0),
      ("STEER_ANGLE", "STEER_TORQUE_SENSOR", 0),
      ("TURN_SIGNALS", "STEERING_LEVERS", 3),   # 3 is no blinkers
      ("LKA_STATE", "EPS_STATUS", 0),
      ("AUTO_HIGH_BEAM", "LIGHT_STALK", 0),
    ]

    checks = [
      ("GEAR_PACKET", 1),
      ("LIGHT_STALK", 1),
      ("STEERING_LEVERS", 0.15),
      ("SEATS_DOORS", 3),
      ("ESP_CONTROL", 3),
      ("EPS_STATUS", 25),
      ("BRAKE_MODULE", 40),
      ("GAS_PEDAL", 33),
      ("WHEEL_SPEEDS", 80),
      ("STEER_ANGLE_SENSOR", 80),
      ("PCM_CRUISE", 33),
      ("STEER_TORQUE_SENSOR", 50),
    ]

    if CP.carFingerprint in [CAR.LEXUS_IS, CAR.LEXUS_RC]:
      signals.append(("MAIN_ON", "DSU_CRUISE", 0))
      signals.append(("SET_SPEED", "DSU_CRUISE", 0))
      checks.append(("DSU_CRUISE", 5))
    else:
      signals.append(("MAIN_ON", "PCM_CRUISE_2", 0))
      signals.append(("SET_SPEED", "PCM_CRUISE_2", 0))
      signals.append(("LOW_SPEED_LOCKOUT", "PCM_CRUISE_2", 0))
      checks.append(("PCM_CRUISE_2", 33))

    if CP.hasZss:
      signals += [("ZORRO_STEER", "SECONDARY_STEER_ANGLE", 0)]
      checks += [("SECONDARY_STEER_ANGLE", 0)]
  
    # add gas interceptor reading if we are using it
    if CP.enableGasInterceptor:
      signals.append(("INTERCEPTOR_GAS", "GAS_SENSOR", 0))
      signals.append(("INTERCEPTOR_GAS2", "GAS_SENSOR", 0))
      checks.append(("GAS_SENSOR", 50))

    if CP.enableBsm:
      signals += [
        ("L_ADJACENT", "BSM", 0),
        ("L_APPROACHING", "BSM", 0),
        ("R_ADJACENT", "BSM", 0),
        ("R_APPROACHING", "BSM", 0),
      ]
      checks += [
        ("BSM", 1)
      ]

    # KRKeegan - Add support for toyota distance button
    signals.append(("DISTANCE_LINES", "PCM_CRUISE_SM", 0))
    checks.append(("PCM_CRUISE_SM", 1))

    if CP.smartDsu:
       signals.append(("FD_BUTTON", "SDSU", 0))
       checks.append(("SDSU", 33))

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 0)

  @staticmethod
  def get_cam_can_parser(CP):

    signals = [
      ("FORCE", "PRE_COLLISION", 0),
      ("PRECOLLISION_ACTIVE", "PRE_COLLISION", 0),
      ("SET_ME_X01", "LKAS_HUD", 0),
      ("LDA_ON_MESSAGE", "LKAS_HUD", 0),
    ]

    # use steering message to check if panda is connected to frc
    checks = [
      ("STEERING_LKA", 42),
      ("PRE_COLLISION", 0), # TODO: figure out why freq is inconsistent
    ]

    if CP.carFingerprint in TSS2_CAR:
      signals.append(("ACC_TYPE", "ACC_CONTROL", 0))
      checks.append(("ACC_CONTROL", 33))

      # KRKeegan - Add support for toyota distance button
      signals.append(("DISTANCE", "ACC_CONTROL", 0))

      # AleSato Automatic BrakeHold
      signals += [
        ("DSS1GDRV", "PRE_COLLISION_2", 0),
        ("DS1STAT2", "PRE_COLLISION_2", 0),
        ("DS1STBK2", "PRE_COLLISION_2", 0),
        ("PCSWAR", "PRE_COLLISION_2", 0),
        ("PCSALM", "PRE_COLLISION_2", 0),
        ("PCSOPR", "PRE_COLLISION_2", 0),
        ("PCSABK", "PRE_COLLISION_2", 0),
        ("PBATRGR", "PRE_COLLISION_2", 0),
        ("PPTRGR", "PRE_COLLISION_2", 0),
        ("IBTRGR", "PRE_COLLISION_2", 0),
        ("CLEXTRGR", "PRE_COLLISION_2", 0),
        ("IRLT_REQ", "PRE_COLLISION_2", 0),
        ("BRKHLD", "PRE_COLLISION_2", 0),
        ("AVSTRGR", "PRE_COLLISION_2", 0),
        ("VGRSTRGR", "PRE_COLLISION_2", 0),
        ("PREFILL", "PRE_COLLISION_2", 0),
        ("PBRTRGR", "PRE_COLLISION_2", 0),
        ("PCSDIS", "PRE_COLLISION_2", 0),
        ("PBPREPMP", "PRE_COLLISION_2", 0),
      ]
      checks.append(("PRE_COLLISION_2", 33))

    return CANParser(DBC[CP.carFingerprint]["pt"], signals, checks, 2, enforce_checks=False)
