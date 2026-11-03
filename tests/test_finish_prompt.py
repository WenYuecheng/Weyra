from weyra.online_skill_evolution import MAINTENANCE_INSTRUCTION


def test_maintenance_documents_empty_replacement_and_scope():
    assert '"new":""' in MAINTENANCE_INSTRUCTION
    assert 'delete' in MAINTENANCE_INSTRUCTION.lower()
    assert 'boundary' in MAINTENANCE_INSTRUCTION
    assert 'At most two edits' in MAINTENANCE_INSTRUCTION
