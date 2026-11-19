metadata = {
    "protocolName": 'Reservoir to plate mix',
    "description": 'Fill a 24-well plate with water and dye from a 12-well reservoir and mix',
    "apiLevel": '2.16',
}


def run(protocol):
    plate = protocol.load_labware('corning_24_wellplate_3.4ml_flat', 1)
    reservoir = protocol.load_labware('nest_12_reservoir_15ml', 2)
    tips_300 = protocol.load_labware('opentrons_96_tiprack_300ul', 3)
    tips_20 = protocol.load_labware('opentrons_96_tiprack_20ul', 4)

    pipette_left = protocol.load_instrument('p300_single_gen2', 'left', tip_racks=[tips_300])
    pipette_right = protocol.load_instrument('p20_single_gen2', 'right', tip_racks=[tips_20])

    liquids = {
        'water': protocol.define_liquid('water', description='', display_color=None),
        'dye': protocol.define_liquid('dye', description='', display_color=None),
    }
    reservoir["A1"].load_liquid(liquid=liquids['water'], volume=5000)
    reservoir["A2"].load_liquid(liquid=liquids['dye'], volume=1000)

    protocol.comment('Pre-fill row A with water')
    pipette_left.distribute(250, reservoir["A1"], [plate["A1"], plate["A2"], plate["A3"], plate["A4"], plate["A5"], plate["A6"]], new_tip='once')
    protocol.comment('Spike dye into row A using p20 pipette')
    pipette_right.transfer(15, reservoir["A2"], [plate["A1"], plate["A2"], plate["A3"], plate["A4"], plate["A5"], plate["A6"]], new_tip='always')
    protocol.comment('Mix each well in row A')
    pipette_left.pick_up_tip()
    pipette_left.mix(4, 150, plate["A1"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(4, 150, plate["A2"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(4, 150, plate["A3"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(4, 150, plate["A4"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(4, 150, plate["A5"])
    pipette_left.drop_tip()
    pipette_left.pick_up_tip()
    pipette_left.mix(4, 150, plate["A6"])
    pipette_left.drop_tip()
    protocol.delay(seconds=3)
