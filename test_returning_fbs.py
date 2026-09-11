from datetime import datetime
from zoneinfo import ZoneInfo
import build

def prop(kind, value):
    return {'type': kind, kind: value}

def board(rid, day, gtg='No', studio='527', status='Upcoming'):
    return {'id': rid, 'properties': {
        '🎨 Artist Database': prop('relation', [{'id': 'artistbest'}]),
        'Studio': prop('select', {'name': studio}),
        'Booking Date': prop('date', {'start': day}),
        'Start Time': prop('rich_text', [{'plain_text': '16:00'}]),
        'Type of Booking': prop('select', {'name': 'FBS'}),
        'Booking Status': prop('status', {'name': status}),
        'HTA': prop('status', {'name': 'Sent'}),
        'GTG': prop('status', {'name': gtg})}}

def test_returning_fbs_history_reaches_watchdog_and_display():
    now = datetime(2026, 9, 11, 13, tzinfo=ZoneInfo('America/Toronto'))
    current = board('current', '2026-09-11')
    past = board('prior', '2026-07-26', 'Yes', status='Complete')
    original = build._notion_query
    build._notion_query = lambda token, ds, body: [past] if 'and' in body.get('filter', {}) else [current]
    try:
        bookings = build.fetch_hta_watch_bookings('fixture', now.date())
        reminder = dict(id='delivered', artist='artistbest', studio='527', shape='RA',
                        status='Sent', receipt=True, sent_at='2026-09-10T13:01:00Z',
                        created='2026-09-10T04:00:00Z', linked=False)
        assert build.hta_verdicts(bookings, [reminder], now)[0]['state'] == 'verified'
        display = build.parse_notion(build.fetch_notion_rows('fixture', '2026-09-11'))
        assert display[0]['gtg'] is True
        for invalid in [board('prior', '2026-07-26', 'Yes', studio='901'),
                        board('prior', '2026-07-26', 'No'),
                        board('prior', '2026-09-12', 'Yes'),
                        board('prior', '2026-07-26', 'Yes', status='Cancelled')]:
            past.clear(); past.update(invalid)
            bookings = build.fetch_hta_watch_bookings('fixture', now.date())
            assert build.hta_verdicts(bookings, [reminder], now)[0]['state'] == 'missing'
        past.clear(); past.update(board('prior', '2026-07-26', 'Yes'))
        past['properties']['HTA'] = prop('status', {'name': 'Unsent'})
        bookings = build.fetch_hta_watch_bookings('fixture', now.date())
        assert build.hta_verdicts(bookings, [reminder], now)[0]['state'] == 'missing'
    finally:
        build._notion_query = original

if __name__ == '__main__':
    test_returning_fbs_history_reaches_watchdog_and_display()
    print('returning FBS regression passed')
