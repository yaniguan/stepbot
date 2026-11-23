metadata = {
    "protocolName": 'Two-fold dilution series',
    "description": 'Two-fold serial dilution of dye in buffer across row A',
    "apiLevel": '2.16',
}


def run(protocol):
    plate = protocol.load_labware('corning_96_wellplate_360ul_flat', 1)
    reservoir = protocol.load_labware('nest_12_reservoir_15ml', 2)
    tips = protocol.load_labware('opentrons_96_tiprack_300ul', 3)

    pipette_left = protocol.load_instrument('p300_single_gen2', 'left', tip_racks=[tips])

    liquids = {
        'dye': protocol.define_liquid('dye', description='', display_color=None),
        'buffer': protocol.define_liquid('buffer', description='', display_color=None),
    }
    reservoir["A1"].load_liquid(liquid=liquids['dye'], volume=2000)
    reservoir["A2"].load_liquid(liquid=liquids['buffer'], volume=5000)

    protocol.comment('Prepare 2-fold dye dilution series across row A')
    pipette_left.distribute(100, reservoir["A2"], [plate["A2"], plate["A3"], plate["A4"], plate["A5"], plate["A6"], plate["A7"], plate["A8"]], new_tip='once')
    pipette_left.transfer(200, reservoir["A1"], plate["A1"], new_tip='once')
    pipette_left.pick_up_tip()
    pipette_left.transfer(100, plate["A1"], plate["A2"], new_tip='never', mix_after=(3, 100))
    pipette_left.transfer(100, plate["A2"], plate["A3"], new_tip='never', mix_after=(3, 100))
    pipette_left.transfer(100, plate["A3"], plate["A4"], new_tip='never', mix_after=(3, 100))
    pipette_left.transfer(100, plate["A4"], plate["A5"], new_tip='never', mix_after=(3, 100))
    pipette_left.transfer(100, plate["A5"], plate["A6"], new_tip='never', mix_after=(3, 100))
    pipette_left.transfer(100, plate["A6"], plate["A7"], new_tip='never', mix_after=(3, 100))
    pipette_left.transfer(100, plate["A7"], plate["A8"], new_tip='never', mix_after=(3, 100))
    pipette_left.aspirate(100, plate["A8"])
    pipette_left.dispense(100, protocol.fixed_trash)
    pipette_left.drop_tip()
    protocol.delay(seconds=5)
