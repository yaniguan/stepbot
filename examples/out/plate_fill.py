metadata = {
    "protocolName": 'Plate fill for absorbance read',
    "description": 'Distribute buffer and add dye or water across row A for a plate read',
    "apiLevel": '2.16',
}


def run(protocol):
    plate = protocol.load_labware('corning_96_wellplate_360ul_flat', 1)
    reservoir = protocol.load_labware('nest_12_reservoir_15ml', 2)
    tips = protocol.load_labware('opentrons_96_tiprack_300ul', 3)

    pipette_left = protocol.load_instrument('p300_single_gen2', 'left', tip_racks=[tips])

    protocol.comment('Distribute buffer into row A wells A1-A8')
    pipette_left.distribute(150, reservoir["A1"], [plate["A1"], plate["A2"], plate["A3"], plate["A4"], plate["A5"], plate["A6"], plate["A7"], plate["A8"]], new_tip='once')
    protocol.comment('Add dye to A1-A4 and water blanks to A5-A8')
    pipette_left.transfer(50, reservoir["A2"], [plate["A1"], plate["A2"], plate["A3"], plate["A4"]], new_tip='always')
    pipette_left.transfer(50, reservoir["A3"], [plate["A5"], plate["A6"], plate["A7"], plate["A8"]], new_tip='once')
    pipette_left.pick_up_tip()
    pipette_left.mix(3, 100, plate["A1"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(3, 100, plate["A2"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(3, 100, plate["A3"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(3, 100, plate["A4"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(3, 100, plate["A5"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(3, 100, plate["A6"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(3, 100, plate["A7"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(3, 100, plate["A8"])
    pipette_left.drop_tip()
    protocol.delay(seconds=10)
