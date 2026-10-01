"""Lightweight, explicit event interpretations; not full standard validators.

Reference: hl7apy 2.5.1, revision recorded in THIRD_PARTY_NOTICES.md.
"""
from .structure import Grammar, GroupRule


def paired_order(context):
    """ORC opens; first OBR joins; subsequent OBR opens a sibling."""
    name = context.segment.name
    if name == 'ORC':
        return True
    if name != 'OBR':
        return False
    current = context.current
    if current is not None and current.name == 'FINANCIAL':
        return False  # The active financial container owns its own order boundaries.
    if current is None or current.name not in ('ORDER', 'ORDER_OBSERVATION', 'COMMON_ORDER'):
        return True
    # During resolution the current range is open, so use raw direct members.
    def contains_obr(group):
        return any(s.name == 'OBR' for s in group._segments) or any(contains_obr(child) for child in group._children)
    return contains_obr(current)


def patient_result(context):
    return context.segment.name == 'PID' or (context.segment.name in ('ORC', 'OBR') and context.current is None)


OBSERVATION = GroupRule('OBSERVATION', 'OBX', end_before=('ORC', 'OBR', 'SPM', 'FT1', 'CTI', 'DSC', 'PID', 'PV1', 'AL1', 'DG1', 'PR1', 'IN1'))
INSURANCE = GroupRule('INSURANCE', 'IN1', end_before=('GT1', 'AL1', 'ACC', 'UB1', 'UB2', 'PDA', 'PID', 'ORC', 'OBR'))
PROCEDURE = GroupRule('PROCEDURE', 'PR1', end_before=('GT1', 'IN1', 'ACC', 'PID', 'ORC', 'FT1'))
VISIT = GroupRule('PATIENT_VISIT', 'PV1', end_before=('DB1', 'OBX', 'AL1', 'DG1', 'PR1', 'GT1', 'IN1', 'ORC', 'OBR', 'FT1', 'RGS', 'PID'))

ADT_A01 = Grammar((GroupRule('PATIENT', 'PID', children=(VISIT, OBSERVATION, PROCEDURE, INSURANCE)),))
SIU_S12 = Grammar((GroupRule('APPOINTMENT', 'SCH', children=(
    GroupRule('PATIENT', 'PID', end_before=('RGS',), children=(VISIT, OBSERVATION)),
    GroupRule('RESOURCES', 'RGS', children=(
        GroupRule('SERVICE', 'AIS'), GroupRule('GENERAL_RESOURCE', 'AIG'),
        GroupRule('LOCATION_RESOURCE', 'AIL'), GroupRule('PERSONNEL_RESOURCE', 'AIP'),
    )),
)),))
ORM_O01 = Grammar((
    GroupRule('PATIENT', 'PID', end_before=('ORC', 'OBR'), children=(VISIT, INSURANCE)),
    GroupRule('ORDER', paired_order, children=(
        GroupRule('ORDER_DETAIL', ('OBR', 'RQD', 'RQ1', 'RXO', 'ODS', 'ODT'), end_before=('ORC', 'FT1', 'BLG'), children=(OBSERVATION,)),
    )),
))
ORU_R01 = Grammar((GroupRule('PATIENT_RESULT', patient_result, end_before=('DSC',), children=(
    GroupRule('PATIENT', 'PID', end_before=('ORC', 'OBR'), children=(VISIT,)),
    GroupRule('ORDER_OBSERVATION', paired_order, children=(
        GroupRule('TIMING_QUANTITY', 'TQ1', end_before=('CTD', 'OBX', 'SPM', 'FT1', 'CTI')),
        OBSERVATION,
        GroupRule('SPECIMEN', 'SPM', children=(OBSERVATION,)),
    )),
)),))
COMMON_ORDER = GroupRule('COMMON_ORDER', paired_order, children=(
    GroupRule('TIMING_QUANTITY', 'TQ1', end_before=('OBR', 'OBX')),
    OBSERVATION,
))
DFT_P03 = Grammar((GroupRule('PATIENT', 'PID', children=(
    VISIT,
    GroupRule('FINANCIAL', 'FT1', end_before=('DG1', 'DRG', 'GT1', 'IN1', 'ACC'), children=(PROCEDURE, COMMON_ORDER)),
    COMMON_ORDER, INSURANCE,
)),))
