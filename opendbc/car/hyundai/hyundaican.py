import crcmod
from opendbc.car.hyundai.values import CAR, HyundaiFlags

hyundai_checksum = crcmod.mkCrcFun(0x11D, initCrc=0xFD, rev=False, xorOut=0xdf)

def create_lkas11(packer, frame, CP, apply_torque, steer_req,
                  torque_fault, lkas11, sys_warning, sys_state, enabled,
                  left_lane, right_lane,
                  left_lane_depart, right_lane_depart):
  values = {s: lkas11[s] for s in [
    "CF_Lkas_LdwsActivemode",
    "CF_Lkas_LdwsSysState",
    "CF_Lkas_SysWarning",
    "CF_Lkas_LdwsLHWarning",
    "CF_Lkas_LdwsRHWarning",
    "CF_Lkas_HbaLamp",
    "CF_Lkas_FcwBasReq",
    "CF_Lkas_HbaSysState",
    "CF_Lkas_FcwOpt",
    "CF_Lkas_HbaOpt",
    "CF_Lkas_FcwSysState",
    "CF_Lkas_FcwCollisionWarning",
    "CF_Lkas_FusionState",
    "CF_Lkas_FcwOpt_USM",
    "CF_Lkas_LdwsOpt_USM",
  ]}
  values["CF_Lkas_LdwsSysState"] = sys_state
  values["CF_Lkas_SysWarning"] = 0 # 3 if sys_warning else 0
  values["CF_Lkas_LdwsLHWarning"] = left_lane_depart
  values["CF_Lkas_LdwsRHWarning"] = right_lane_depart
  values["CR_Lkas_StrToqReq"] = apply_torque
  values["CF_Lkas_ActToi"] = steer_req
  values["CF_Lkas_ToiFlt"] = torque_fault  # seems to allow actuation on CR_Lkas_StrToqReq
  values["CF_Lkas_MsgCount"] = frame % 0x10

  if CP.flags & HyundaiFlags.SEND_LFA.value:
    values["CF_Lkas_LdwsActivemode"] = int(left_lane) + (int(right_lane) << 1)
    values["CF_Lkas_LdwsOpt_USM"] = 2

    # FcwOpt_USM 5 = Orange blinking car + lanes
    # FcwOpt_USM 4 = Orange car + lanes
    # FcwOpt_USM 3 = Green blinking car + lanes
    # FcwOpt_USM 2 = Green car + lanes
    # FcwOpt_USM 1 = White car + lanes
    # FcwOpt_USM 0 = No car + lanes
    values["CF_Lkas_FcwOpt_USM"] = 2 if enabled else 1

    # SysWarning 4 = keep hands on wheel
    # SysWarning 5 = keep hands on wheel (red)
    # SysWarning 6 = keep hands on wheel (red) + beep
    # Note: the warning is hidden while the blinkers are on
    values["CF_Lkas_SysWarning"] = 0 #4 if sys_warning else 0

  # Likely cars lacking the ability to show individual lane lines in the dash
  elif CP.carFingerprint in (CAR.KIA_OPTIMA_G4, CAR.KIA_OPTIMA_G4_FL):
    # SysWarning 4 = keep hands on wheel + beep
    values["CF_Lkas_SysWarning"] = 4 if sys_warning else 0

    # SysState 0 = no icons
    # SysState 1-2 = white car + lanes
    # SysState 3 = green car + lanes, green steering wheel
    # SysState 4 = green car + lanes
    values["CF_Lkas_LdwsSysState"] = 3 if enabled else 1
    values["CF_Lkas_LdwsOpt_USM"] = 2  # non-2 changes above SysState definition

    # these have no effect
    values["CF_Lkas_LdwsActivemode"] = 0
    values["CF_Lkas_FcwOpt_USM"] = 0

  elif CP.carFingerprint == CAR.HYUNDAI_GENESIS:
    # This field is actually LdwsActivemode
    # Genesis and Optima fault when forwarding while engaged
    values["CF_Lkas_LdwsActivemode"] = 2

  dat = packer.make_can_msg("LKAS11", 0, values)[1]

  if CP.flags & HyundaiFlags.CHECKSUM_CRC8:
    # CRC Checksum as seen on 2019 Hyundai Santa Fe
    dat = dat[:6] + dat[7:8]
    checksum = hyundai_checksum(dat)
  elif CP.flags & HyundaiFlags.CHECKSUM_6B:
    # Checksum of first 6 Bytes, as seen on 2018 Kia Sorento
    checksum = sum(dat[:6]) % 256
  else:
    # Checksum of first 6 Bytes and last Byte as seen on 2018 Kia Stinger
    checksum = (sum(dat[:6]) + dat[7]) % 256

  values["CF_Lkas_Chksum"] = checksum

  return packer.make_can_msg("LKAS11", 0, values)


def create_clu11(packer, frame, clu11, button, CP):
  values = {s: clu11[s] for s in [
    "CF_Clu_CruiseSwState",
    "CF_Clu_CruiseSwMain",
    "CF_Clu_SldMainSW",
    "CF_Clu_ParityBit1",
    "CF_Clu_VanzDecimal",
    "CF_Clu_Vanz",
    "CF_Clu_SPEED_UNIT",
    "CF_Clu_DetentOut",
    "CF_Clu_RheostatLevel",
    "CF_Clu_CluInfo",
    "CF_Clu_AmpInfo",
    "CF_Clu_AliveCnt1",
  ]}
  values["CF_Clu_CruiseSwState"] = button
  values["CF_Clu_AliveCnt1"] = frame % 0x10
  # send buttons to camera on camera-scc based cars
  bus = 2 if CP.flags & HyundaiFlags.CAMERA_SCC else 0
  return packer.make_can_msg("CLU11", bus, values)


def create_lfahda_mfc(packer, CC, blinking_signal):
  activeCarrot = CC.hudControl.activeCarrot
  values = {
    "LFA_Icon_State": 2 if CC.latActive else 1 if CC.enabled else 0,
    #"HDA_Active": 1 if activeCarrot >= 2 else 0,
    #"HDA_Icon_State": 2 if activeCarrot == 3 and blinking_signal else 2 if activeCarrot >= 2 else 0,
    "HDA_Icon_State": 0 if activeCarrot == 3 and blinking_signal else 2 if activeCarrot >= 1 else 0,
    "HDA_VSetReq": 0, #set_speed_in_units if activeCarrot >= 2 else 0,
    "HDA_USM" : 2,
    "HDA_Icon_Wheel" : 1 if CC.latActive else 0,
    #"HDA_Chime" : 1 if CC.latActive else 0, # comment for K9 chime, 
  }
  return packer.make_can_msg("LFAHDA_MFC", 0, values)

def create_acc_commands_scc(packer, enabled, accel, jerk, idx, hud_control, set_speed, stopping, long_override, use_fca, CS, soft_hold_mode):
  from opendbc.car.hyundai.carcontroller import HyundaiJerk
  cruise_available = CS.out.cruiseState.available
  if CS.paddle_button_prev > 0:
    cruise_available = False
  soft_hold_active = CS.softHoldActive
  soft_hold_info = soft_hold_active > 1 and enabled
  #soft_hold_mode = 2 ## some cars can't enable while braking
  long_enabled = enabled or (soft_hold_active > 0 and soft_hold_mode == 2)
  stop_req = 1 if stopping or (soft_hold_active > 0 and soft_hold_mode == 2) else 0
  d = hud_control.leadDistance
  objGap = 0 if d == 0 else 2 if d < 25 else 3 if d < 40 else 4 if d < 70 else 5 
  objGap2 = 0 if objGap == 0 else 2 if hud_control.leadRelSpeed < -0.2 else 1

  if long_enabled:
    scc12_acc_mode = 2 if long_override else 1
    scc14_acc_mode = 2 if long_override else 1
    if CS.out.brakeHoldActive:
      scc12_acc_mode = 0
      scc14_acc_mode = 4
    elif CS.out.brakePressed:
      scc12_acc_mode = 1
      scc14_acc_mode = 1
  else:
    scc12_acc_mode = 0
    scc14_acc_mode = 4

  warning_front = False

  commands = []
  if CS.scc11 is not None:
    values = CS.scc11
    values["MainMode_ACC"] = 1 if cruise_available else 0
    values["TauGapSet"] = hud_control.leadDistanceBars
    values["VSetDis"] = set_speed if enabled else 0
    values["AliveCounterACC"] = idx % 0x10
    values["SCCInfoDisplay"] = 3 if warning_front else 4 if soft_hold_info else 0 if enabled else 0   #2: 크루즈 선택, 3: 전방상황주의, 4: 출발준비
    values["ObjValid"] = 1 if hud_control.leadVisible else 0
    values["ACC_ObjStatus"] = 1 if hud_control.leadVisible else 0
    values["ACC_ObjLatPos"] = 0
    values["ACC_ObjRelSpd"] = hud_control.leadRelSpeed
    values["ACC_ObjDist"] = int(hud_control.leadDistance)
    values["DriverAlertDisplay"] = 0
    commands.append(packer.make_can_msg("SCC11", 0, values))
    
  if CS.scc12 is not None:
    values = CS.scc12
    values["ACCMode"] = scc12_acc_mode #2 if enabled and long_override else 1 if long_enabled else 0
    values["StopReq"] = stop_req
    values["aReqRaw"] = accel
    values["aReqValue"] = accel
    values["ACCFailInfo"] = 0

    #values["DESIRED_DIST"] = CS.out.vEgo * 1.0 + 4.0  # TF: 1.0 + STOPDISTANCE 4.0 m로 가정함.

    values["CR_VSM_ChkSum"] = 0
    values["CR_VSM_Alive"] = idx % 0xF
    scc12_dat = packer.make_can_msg("SCC12", 0, values)[1]
    values["CR_VSM_ChkSum"] = 0x10 - sum(sum(divmod(i, 16)) for i in scc12_dat) % 0x10

    commands.append(packer.make_can_msg("SCC12", 0, values))

  if CS.scc14 is not None:
    values = CS.scc14
    values["ComfortBandUpper"] = jerk.cb_upper
    values["ComfortBandLower"] = jerk.cb_lower
    values["JerkUpperLimit"] = jerk.jerk_u
    values["JerkLowerLimit"] = jerk.jerk_l if long_enabled else 0 # for KONA test
    values["ACCMode"] = scc14_acc_mode #2 if enabled and long_override else 1 if long_enabled else 4 # stock will always be 4 instead of 0 after first disengage
    values["ObjGap"] = objGap #2 if hud_control.leadVisible else 0 # 5: >30, m, 4: 25-30 m, 3: 20-25 m, 2: < 20 m, 0: no lead
    values["ObjDistStat"] = objGap2
    commands.append(packer.make_can_msg("SCC14", 0, values))

  # Only send FCA11 on cars where it exists on the bus
  if False: #use_fca:
    # note that some vehicles most likely have an alternate checksum/counter definition
    # https://github.com/commaai/opendbc/commit/9ddcdb22c4929baf310295e832668e6e7fcfa602
    fca11_values = {
      "CR_FCA_Alive": idx % 0xF,
      "PAINT1_Status": 1,
      "FCA_DrvSetStatus": 1,
      "FCA_Status": 1,  # AEB disabled
    }
    fca11_dat = packer.make_can_msg("FCA11", 0, fca11_values)[1]
    fca11_values["CR_FCA_ChkSum"] = hyundai_checksum(fca11_dat[:7])
    commands.append(packer.make_can_msg("FCA11", 0, fca11_values))

  return commands

def create_acc_opt_copy(CS, packer):
  return packer.make_can_msg("SCC13", 0, CS.scc13)

def create_acc_commands(packer, enabled, accel, jerk, idx, hud_control, set_speed, stopping, long_override, use_fca, CP, CS, soft_hold_mode):
  from opendbc.car.hyundai.carcontroller import HyundaiJerk
  cruise_available = CS.out.cruiseState.available
  soft_hold_active = CS.softHoldActive
  soft_hold_info = soft_hold_active > 1 and enabled
  #soft_hold_mode = 2 ## some cars can't enable while braking
  long_enabled = enabled or (soft_hold_active > 0 and soft_hold_mode == 2)
  stop_req = 1 if stopping or (soft_hold_active > 0 and soft_hold_mode == 2) else 0
  d = hud_control.leadDistance
  objGap = 0 if d == 0 else 2 if d < 25 else 3 if d < 40 else 4 if d < 70 else 5 
  objGap2 = 0 if objGap == 0 else 2 if hud_control.leadRelSpeed < -0.2 else 1

  if long_enabled:
    scc12_acc_mode = 2 if long_override else 1
    scc14_acc_mode = 2 if long_override else 1
    if CS.out.brakeHoldActive:
      scc12_acc_mode = 0
      scc14_acc_mode = 4
    elif CS.out.brakePressed:
      scc12_acc_mode = 1
      scc14_acc_mode = 1
  else:
    scc12_acc_mode = 0
    scc14_acc_mode = 4

  warning_front = False

  commands = []

  scc11_values = {
    "MainMode_ACC": 1 if cruise_available else 0,
    "TauGapSet": hud_control.leadDistanceBars,
    "VSetDis": set_speed if enabled else 0,
    "AliveCounterACC": idx % 0x10,
    "SCCInfoDisplay": 3 if warning_front else 4 if soft_hold_info else 0 if enabled else 0,   
    "ObjValid": 1 if hud_control.leadVisible else 0, # close lead makes controls tighter
    "ACC_ObjStatus": 1 if hud_control.leadVisible else 0, # close lead makes controls tighter
    "ACC_ObjLatPos": 0,
    "ACC_ObjRelSpd": hud_control.leadRelSpeed,
    "ACC_ObjDist": int(hud_control.leadDistance), # close lead makes controls tighter
    "DriverAlertDisplay": 0,
    }
  commands.append(packer.make_can_msg("SCC11", 0, scc11_values))

  scc12_values = {
    "ACCMode": scc12_acc_mode,
    "StopReq": stop_req,
    "aReqRaw": 0 if stop_req > 0 else accel,
    "aReqValue": accel,  # stock ramps up and down respecting jerk limit until it reaches aReqRaw
    #"DESIRED_DIST": CS.out.vEgo * 1.0 + 4.0,
    "CR_VSM_Alive": idx % 0xF,
  }

  # show AEB disabled indicator on dash with SCC12 if not sending FCA messages.
  # these signals also prevent a TCS fault on non-FCA cars with alpha longitudinal
  if not use_fca:
    scc12_values["CF_VSM_ConfMode"] = 1
    scc12_values["AEB_Status"] = 1  # AEB disabled

  scc12_dat = packer.make_can_msg("SCC12", 0, scc12_values)[1]
  scc12_values["CR_VSM_ChkSum"] = 0x10 - sum(sum(divmod(i, 16)) for i in scc12_dat) % 0x10

  commands.append(packer.make_can_msg("SCC12", 0, scc12_values))

  scc14_values = {
    "ComfortBandUpper": jerk.cb_upper, # stock usually is 0 but sometimes uses higher values
    "ComfortBandLower": jerk.cb_lower, # stock usually is 0 but sometimes uses higher values
    "JerkUpperLimit": jerk.jerk_u, # stock usually is 1.0 but sometimes uses higher values
    "JerkLowerLimit": jerk.jerk_l, # stock usually is 0.5 but sometimes uses higher values
    "ACCMode": scc14_acc_mode, # if enabled and long_override else 1 if enabled else 4, # stock will always be 4 instead of 0 after first disengage
    "ObjGap": objGap, #2 if hud_control.leadVisible else 0, # 5: >30, m, 4: 25-30 m, 3: 20-25 m, 2: < 20 m, 0: no lead
    "ObjDistStat": objGap2,
  }
  commands.append(packer.make_can_msg("SCC14", 0, scc14_values))

  # Only send FCA11 on cars where it exists on the bus
  # On Camera SCC cars, FCA11 is not disabled, so we forward stock FCA11 back to the car forward hooks
  if use_fca and not (CP.flags & HyundaiFlags.CAMERA_SCC):
    # note that some vehicles most likely have an alternate checksum/counter definition
    # https://github.com/commaai/opendbc/commit/9ddcdb22c4929baf310295e832668e6e7fcfa602
    fca11_values = {
      "CR_FCA_Alive": idx % 0xF,
      "PAINT1_Status": 1,
      "FCA_DrvSetStatus": 1,
      "FCA_Status": 1,  # AEB disabled
    }
    fca11_dat = packer.make_can_msg("FCA11", 0, fca11_values)[1]
    fca11_values["CR_FCA_ChkSum"] = hyundai_checksum(fca11_dat[:7])
    commands.append(packer.make_can_msg("FCA11", 0, fca11_values))

  return commands

def create_acc_opt(packer, CP):
  commands = []

  scc13_values = {
    "SCCDrvModeRValue": 2,
    "SCC_Equip": 1,
    "Lead_Veh_Dep_Alert_USM": 2,
  }
  commands.append(packer.make_can_msg("SCC13", 0, scc13_values))

  # TODO: this needs to be detected and conditionally sent on unsupported long cars
  # On Camera SCC cars, FCA12 is not disabled, so we forward stock FCA12 back to the car forward hooks
  if not (CP.flags & HyundaiFlags.CAMERA_SCC):
    fca12_values = {
      "FCA_DrvSetState": 2,
      "FCA_USM": 1, # AEB disabled
    }
    commands.append(packer.make_can_msg("FCA12", 0, fca12_values))

  return commands

def create_frt_radar_opt(packer):
  frt_radar11_values = {
    "CF_FCA_Equip_Front_Radar": 1,
  }
  return packer.make_can_msg("FRT_RADAR11", 0, frt_radar11_values)

def create_clu11_button(packer, frame, clu11, button, CP):
  values = clu11
  values["CF_Clu_CruiseSwState"] = button
  #values["CF_Clu_AliveCnt1"] = frame % 0x10
  values["CF_Clu_AliveCnt1"] = (values["CF_Clu_AliveCnt1"] + 1) % 0x10
  # send buttons to camera on camera-scc based cars
  bus = 2 if CP.flags & HyundaiFlags.CAMERA_SCC else 0
  return packer.make_can_msg("CLU11", bus, values)

def create_mdps12(packer, frame, mdps12):
  values = mdps12
  values["CF_Mdps_ToiActive"] = 0
  values["CF_Mdps_ToiUnavail"] = 1
  values["CF_Mdps_MsgCount2"] = frame % 0x100
  values["CF_Mdps_Chksum2"] = 0

  dat = packer.make_can_msg("MDPS12", 2, values)[1]
  checksum = sum(dat) % 256
  values["CF_Mdps_Chksum2"] = checksum

  return packer.make_can_msg("MDPS12", 2, values)
