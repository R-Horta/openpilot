def create_steer_command(packer, steer, steer_req, raw_cnt):
  """Creates a CAN message for the Toyota Steer Command."""

  values = {
    "STEER_REQUEST": steer_req,
    "STEER_TORQUE_CMD": steer if steer_req else 0,
    "COUNTER": raw_cnt,
    "SET_ME_1": 1,
  }
  return packer.make_can_msg("STEERING_LKA", 0, values)


def create_lta_steer_command(packer, steer, steer_req, raw_cnt):
  """Creates a CAN message for the Toyota LTA Steer Command."""

  values = {
    "COUNTER": raw_cnt + 128,
    "SETME_X1": 1,
    "SETME_X3": 3,
    "PERCENTAGE": 100,
    "SETME_X64": 0x64,
    "ANGLE": 0,  # Rate limit? Lower values seeem to work better, but needs more testing
    "STEER_ANGLE_CMD": steer,
    "STEER_REQUEST": steer_req,
    "STEER_REQUEST_2": steer_req,
    "BIT": 0,
  }
  return packer.make_can_msg("STEERING_LTA", 0, values)


def create_accel_command(packer, accel, pcm_cancel, standstill_req, lead, acc_type, distance, long_press):
  # TODO: find the exact canceling bit that does not create a chime
  values = {
    "ACCEL_CMD": accel,
    "ACC_TYPE": acc_type,
    "DISTANCE": distance,
    "MINI_CAR": lead,
    "PERMIT_BRAKING": 1,
    "RELEASE_STANDSTILL": not standstill_req,
    "CANCEL_REQ": pcm_cancel,
    "ALLOW_LONG_PRESS": long_press,
  }
  return packer.make_can_msg("ACC_CONTROL", 0, values)


def create_acc_cancel_command(packer):
  values = {
    "GAS_RELEASED": 0,
    "CRUISE_ACTIVE": 0,
    "STANDSTILL_ON": 0,
    "ACCEL_NET": 0,
    "CRUISE_STATE": 0,
    "CANCEL_REQ": 1,
  }
  return packer.make_can_msg("PCM_CRUISE", 0, values)


def create_fcw_command(packer, fcw):
  values = {
    "PCS_INDICATOR": 1,
    "FCW": fcw,
    "SET_ME_X20": 0x20,
    "SET_ME_X10": 0x10,
    "PCS_OFF": 1,
    "PCS_SENSITIVITY": 0,
  }
  return packer.make_can_msg("ACC_HUD", 0, values)


def create_ui_command(packer, steer, chime, left_line, right_line, left_lane_depart, right_lane_depart, faded_line, use_lta_msg, lkas_active):
  values = {
    "RIGHT_LINE": 2 if faded_line else 3 if right_lane_depart else 1 if right_line else 2,
    "LEFT_LINE": 2 if faded_line else 3 if left_lane_depart else 1 if left_line else 2,
    "BARRIERS" : 1 if lkas_active else 0,
    "SET_ME_X0C": 0x0c,
    "SET_ME_X2C": 0x2c,
    "SET_ME_X38": 0x38,
    "SET_ME_X02": 0x02,
    "SET_ME_X01": 2,
    "SET_ME_X01_2": 1,
    "REPEATED_BEEPS": 0,
    "TWO_BEEPS": 0,
    "LDA_ALERT": steer or chime,
    "LDA_ON_MESSAGE": 1 if use_lta_msg else 0,
  }
  return packer.make_can_msg("LKAS_HUD", 0, values)

def create_ui_command_off(packer, use_lta_msg):
  values = {
    "RIGHT_LINE": 0,
    "LEFT_LINE": 0,
    "BARRIERS" : 0,
    "SET_ME_X0C": 0x0a,
    "SET_ME_X2C": 0x34,
    "SET_ME_X38": 0x00,
    "SET_ME_X02": 0x12,
    "SET_ME_X01": 1 if use_lta_msg else 0,
    "SET_ME_X01_2": 1,
    "REPEATED_BEEPS": 0,
    "TWO_BEEPS": 0,
    "LDA_ALERT": 0,
    "LDA_ON_MESSAGE": 2 if use_lta_msg else 0,
  }
  return packer.make_can_msg("LKAS_HUD", 0, values)

def create_ui_command_disable_startup_lkas(packer, use_lta_msg):
  values = {
    "SET_ME_X01": 1 if use_lta_msg else 0, # LKAS not enabled
    "LDA_ON_MESSAGE": 2 if use_lta_msg else 0,
  }
  return packer.make_can_msg("LKAS_HUD", 0, values)


def create_brakehold_command(packer, stock_AEB, cut3frames):
  values = {
    "DSS1GDRV": 1023,
    "PBRTRGR": cut3frames,
  }
  if len(stock_AEB):
    values.update({s: stock_AEB[s] for s in [
      # They are not all necessary since brakehold only sends stopped,
      # however we will leave prepared for the future alternative AEB of the comma.ai
      "DSS1GDRV",
      "DS1STAT2",
      "DS1STBK2",
      "PCSWAR",
      "PCSALM",
      "PCSOPR",
      "PCSABK",
      "PBATRGR",
      "PPTRGR",
      "IBTRGR",
      "CLEXTRGR",
      "IRLT_REQ",
      "BRKHLD",
      "AVSTRGR",
      "VGRSTRGR",
      "PREFILL",
      "PBRTRGR",
      "PCSDIS",
      "PBPREPMP",
    ]})
  return packer.make_can_msg("PRE_COLLISION_2", 0, values)
