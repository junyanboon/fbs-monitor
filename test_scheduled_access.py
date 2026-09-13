from datetime import datetime, timedelta
from unittest.mock import patch
import build

NOW = datetime(2026, 9, 13, 8, 8, tzinfo=build.TZ)
BOOKING = dict(id='booking-vicky', artist='artistvicky', studio='509A', tier='Monitor', date='2026-09-13', start='14:15', who='Vicky Yeung', hta='Sent')

def raw(status='Ready to Send', body='Door Code: [fixture]', receipt=''):
    def p(kind, value):
        return {'type':kind, kind:([{'plain_text':value}] if kind in ('title','rich_text') else value)}
    return dict(id='queue-vicky', created_time='2026-09-13T04:33:00Z', properties={
        'Message Code':p('title','Returning Access 509A RA-sweep-0913-1415-509A'),
        'Message Body':p('rich_text',body), 'Status':p('status',{'name':status}),
        'Artist':p('relation',[{'id':'artistvicky'}]), 'Studio':p('select',{'name':'509A'}),
        'Send After':p('date',{'start':'2026-09-13T13:00:00Z'}),
        'Dispatch Receipt':p('rich_text',receipt)})

def result(status='Ready to Send', now=NOW, **kw):
    with patch.object(build, '_notion_query',return_value=[raw(status,**kw)]):
        rows=build.fetch_hta_rows('fixture',NOW-timedelta(days=7))
    return build.hta_verdicts([BOOKING],rows,now)[0],rows

def test_scheduled_returning_access_shows_0900_without_claiming_delivery():
    v, rows=result()
    assert v['state']=='scheduled'
    assert build._hta_pill(v)==dict(kind='HTA',state='scheduled',time='09:00')
    assert not rows[0]['receipt']
    for change in ({'tier':'FBS'},{'studio':'509B'},{'artist':'other'}, {'date':'2026-09-14'}):
        b=dict(BOOKING,**change)
        assert build.hta_verdicts([b],rows,NOW)[0]['state']=='missing'

def test_pending_overdue_and_unreceipted_returning_access_are_not_verified():
    assert result('Pending Review')[0]['state']=='awaiting'
    assert result('Error')[0]['state']=='awaiting'
    assert result(now=NOW.replace(hour=9,minute=11))[0]['state']=='stuck'
    assert result('Sent')[0]['state']=='missing'
    assert result(body='Your booking is confirmed')[0]['state']=='missing'

def test_scheduled_message_suppresses_only_its_own_watchdog_access_warning():
    v,_=result()
    event=dict(kind='booking',who='Vicky Yeung',studio='509A',_artist_id='artistvicky',_notion_id='booking-vicky')
    warning=dict(text=build._hta_action_title(BOOKING),artist='artistvicky')
    out=build.flag_access_gaps([dict(event)],[warning],NOW.date(),[v])
    assert not out[0].get('access_gap')
    real=dict(text='Verify Vicky Yeung panel access',artist='artistvicky')
    assert build.flag_access_gaps([dict(event)],[warning,real],NOW.date(),[v])[0]['access_gap']
    assert build.flag_access_gaps([dict(event)],[warning],NOW.date(),[])[0]['access_gap']
    late=dict(v,state='stuck')
    assert build.flag_access_gaps([dict(event)],[warning],NOW.date(),[late])[0]['access_gap']


if __name__ == '__main__':
    test_scheduled_returning_access_shows_0900_without_claiming_delivery()
    test_pending_overdue_and_unreceipted_returning_access_are_not_verified()
    test_scheduled_message_suppresses_only_its_own_watchdog_access_warning()
    print('Scheduled access: 3 regression tests passed')
