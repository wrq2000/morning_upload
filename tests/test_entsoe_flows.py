"""Offline monthly TSV ZIPs, same header as documented by ENTSO-E."""
import asyncio
import io
import zipfile
from datetime import date, timedelta
from unittest import TestCase, mock
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from processes.entsoe_flows import process as p


def points(day, hours=24, minutes=60, value=100, a='BE', b='NL', start_hour=0):
    stamp = p.bounds(day, ZoneInfo(p.TIMEZONES[a])) + timedelta(hours=start_hour)
    return [(stamp + timedelta(minutes=minutes*i), 'PT{}M'.format(minutes), p.EIC[a], p.EIC[b], value, '2026-11-26T00:00:00Z') for i in range(hours*60//minutes)]


def archive(rows, filename='2026_11_PhysicalFlows_12.1.G_r3.csv'):
    raw = pd.DataFrame(rows, columns=p.COLUMNS)
    # Exercise both accepted time formats including day/month ambiguity.
    raw['DateTime(UTC)'] = [x.strftime('%d/%m/%Y %H:%M:%S') if i%2 else x.isoformat() for i,x in enumerate(raw['DateTime(UTC)'])]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('TP_export/'+p.FOLDER+'/'+filename, raw.to_csv(index=False, sep='\t').encode('utf-8-sig'))
    return buf.getvalue(), filename


def daily(rows, day, end=None):
    end = end or day + timedelta(days=1)
    z, name = archive(rows)
    parts = p.read_month(z, name, [('BE','NL')], day, end)
    data, bad = p.latest_points(pd.concat(parts, ignore_index=True))
    return p.daily_frame(data, bad, 'BE','NL',day,end)


class EntsoeTests(TestCase):
    def test_months_and_directions(self):
        self.assertEqual(len(p.get_directions()),144)
        self.assertEqual(p.required_months(date(2026,11,15),date(2026,11,25),p.get_directions()),['2026-11'])
        self.assertEqual(p.required_months(date(2026,12,1),date(2026,12,11),p.get_directions()),['2026-11','2026-12'])
        self.assertEqual(p.required_months(date(2026,12,22),date(2027,1,1),p.get_directions()),['2026-12'])

    def test_dst_and_resolution(self):
        for day,hours,minutes in [(date(2026,3,29),23,60),(date(2026,10,25),25,30),(date(2026,11,24),24,15)]:
            r=daily(points(day,hours,minutes),day).iloc[0]
            self.assertEqual(r.coverage_minutes,hours*60)
            self.assertEqual(r.flow_mw,100)
            self.assertAlmostEqual(r.gas_ecv,100*48/10750)
            self.assertEqual((r.resolution_min,r.resolution_max,r.source_points_count),(minutes,minutes,hours*60//minutes))

    def test_mixed_and_transition(self):
        day=date(2026,11,23)
        rows=points(day)+points(day+timedelta(days=1),12)+points(day+timedelta(days=1),12,15,300,start_hour=12)+points(day+timedelta(days=2),24,15,0)
        with self.assertLogs(p.logger,level='WARNING') as logs:
            f=daily(rows,day,day+timedelta(days=3))
        self.assertEqual(f.flow_mw.tolist(),[100,200,0])
        self.assertEqual(f.source_points_count.tolist(),[24,60,96])
        self.assertEqual(f.resolution_min.tolist(),[60,15,15])
        self.assertEqual(f.resolution_max.tolist(),[60,60,15])
        self.assertEqual(sum('изменение гранулярности' in x for x in logs.output),2)

    def test_missing_zero_and_overlap(self):
        day=date(2026,11,24)
        rows=points(day,24,15,0)
        self.assertEqual(daily(rows,day).iloc[0].flow_mw,0)
        self.assertTrue(daily(rows[:-1],day).empty)
        self.assertTrue(daily(points(day)+[rows[1]],day).empty)
        wrong=list(rows[0]); wrong[1]='PT5M'
        self.assertTrue(daily([tuple(wrong)]+rows[1:],day).empty)

    def test_latest_revision_and_tie(self):
        day=date(2026,11,24)
        rows=points(day)
        newer=list(rows[0]); newer[4]=340; newer[5]='2026-11-27T00:00:00Z'
        f=daily(rows+[rows[1],tuple(newer)],day)
        self.assertEqual(f.iloc[0].flow_mw,110)
        self.assertEqual(f.iloc[0].source_points_count,24)
        clash=list(newer); clash[4]=0
        self.assertTrue(daily(rows+[tuple(newer),tuple(clash)],day).empty)

    def test_filter_and_chunks(self):
        day=date(2026,11,24)
        rows=points(day)+points(day-timedelta(days=1))+points(day,a='FR',b='DE')
        z,name=archive(rows)
        with mock.patch.object(p,'CHUNK_SIZE',7):
            selected=p.read_month(z,name,[('BE','NL')],day,day+timedelta(days=1))
        self.assertEqual(sum(map(len,selected)),24)

    def test_download_once_per_month(self):
        today=date(2026,12,2); start=today-timedelta(days=2)
        rows=points(start)+points(start+timedelta(days=1))
        first=[x for x in rows if x[0].month==11]; second=[x for x in rows if x[0].month==12]
        z1,n1=archive(first); z2,n2=archive(second,'2026_12_PhysicalFlows_12.1.G_r3.csv')
        client=mock.Mock(); client.list_folder.return_value={n1:'id1',n2:'id2'}; client.download_multiple_files_raw.side_effect=[z1,z2]
        with mock.patch.dict('os.environ',{'ENTSOE_LOOKBACK_DAYS':'2'}),mock.patch.object(p,'get_directions',return_value=[('BE','NL'),('NL','BE')]):
            proc=p.EntsoeFlowsProcess(); f,body=asyncio.run(proc.collect(client,today))
        self.assertEqual(len(f),2)
        client.list_folder.assert_called_once_with(p.FOLDER)
        self.assertEqual(client.download_multiple_files_raw.call_args_list,[mock.call(['id1']),mock.call(['id2'])])
        self.assertIn('NL → BE',body)
        self.assertEqual(tuple(f.columns),p.FIELDS)

    def test_retry_and_missing_file(self):
        fn=mock.Mock(side_effect=[requests.Timeout('secret'),b'OK'])
        with mock.patch.object(p.clock,'sleep'):
            self.assertEqual(p.retry_call(fn,2,'test'),b'OK')
        with self.assertRaises(p.SourceError) as err:
            p.retry_call(mock.Mock(side_effect=requests.ConnectionError('secret')),1,'test')
        self.assertNotIn('secret',str(err.exception))
        client=mock.Mock(); client.list_folder.return_value={}
        with self.assertRaises(p.SourceError):
            p.EntsoeFlowsProcess().collect_months(client,date(2026,11,25))
        client.download_multiple_files_raw.assert_not_called()
