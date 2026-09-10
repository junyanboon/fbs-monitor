from datetime import date
from copy import deepcopy
import build

DAY = date(2026, 9, 9)

def event(studio, sid='source-1', nid='room-1', artist='artist'):
    return dict(kind='booking', studio=studio, tier='FBS', start=11., end=13.25,
                _skedda_id=sid, _notion_id=nid, _artist_id=artist,
                _ava_status='Will Not Send', _eob_status='Unsent')

def message(**kw):
    return dict(kind='EOB', studio='509B', artist='artist', status='Pending Review',
                send_after='2026-09-09T17:00:00Z', created='2026-09-09T08:00:00Z', **kw)

def test_one_source_booking_shares_reminder_across_rooms():
    events=[event('509B'), event('527',nid='room-2')]
    result=build.apply_message_dispatch(events,[message()],DAY)
    assert [e['dispatch'] for e in result] == [[dict(kind='EOB',state='awaiting',time='13:00')]]*2

def test_same_artist_same_time_different_booking_does_not_share():
    result=build.apply_message_dispatch([event('509B'),event('527',sid='other')],[message()],DAY)
    assert result[1]['dispatch']==[dict(kind='EOB',state='missing',time=None)]

def test_same_source_different_artist_or_span_does_not_share():
    for other in [event('527',artist='different'),dict(event('527'),end=14.)]:
        result=build.apply_message_dispatch([event('509B'),other],[message()],DAY)
        assert result[1]['dispatch']==[dict(kind='EOB',state='missing',time=None)]

def test_explicit_booking_relation_survives_extension_and_no_schedule():
    r=message(bookings=['room1','room2']);r['send_after']=None
    result=build.apply_message_dispatch([event('527',nid='room-2')],[r],DAY)
    assert result[0]['dispatch']==[dict(kind='EOB',state='awaiting',time=None)]

def test_grouping_does_not_copy_physical_evidence_or_access():
    events=[dict(event('509B'),arrived=11.01,departed=11.2,hta='Sent'),
            dict(event('527',nid='room-2'),arrived=11.25,departed=None,hta='Unsent')]
    before=deepcopy(events)
    build.apply_message_dispatch(events,[message()],DAY)
    for a,b in zip(before,events):
        for field in ('arrived','departed','hta'):
            assert a[field]==b[field]
