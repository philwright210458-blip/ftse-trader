import datetime as dt
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo
from ftse_scheduler import JobClaims, due_jobs

UK=ZoneInfo('Europe/London')
CONFIG={'research':{'interval_minutes':15},'schedule':{'max_lateness_seconds':180,'bar_grace_seconds':90}}

class SchedulerTests(unittest.TestCase):
    def test_holiday_and_weekend_have_no_jobs(self):
        for day in ('2026-12-25','2026-10-17'):
            self.assertEqual(due_jobs(dt.datetime.fromisoformat(day+'T12:00:00').replace(tzinfo=UK),CONFIG),[])

    def test_first_bar_full_session_and_grace(self):
        jobs=due_jobs(dt.datetime(2026,10,16,8,16,30,tzinfo=UK),CONFIG)
        live=[x for x in jobs if not x.missed]
        self.assertEqual([x.kind for x in live],['watch'])
        self.assertTrue(live[0].scheduled_at.endswith('07:16:30+00:00'))
        late=due_jobs(dt.datetime(2026,10,16,16,46,tzinfo=UK),CONFIG)
        self.assertEqual(late[-1].kind,'resolve')
        self.assertFalse(late[-1].missed)

    def test_dst_is_market_local(self):
        for day,expected in ((23,'07:00:00'),(26,'08:00:00')):
            jobs=due_jobs(dt.datetime(2026,10,day,8,0,tzinfo=UK),CONFIG)
            self.assertIn(expected,jobs[-1].scheduled_at)

    def test_claims_no_duplicate_and_no_backdating(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'jobs.sqlite')
            a,b=JobClaims(path),JobClaims(path)
            now=dt.datetime(2026,10,16,8,0,tzinfo=UK)
            jobs=due_jobs(now,CONFIG); job=jobs[-1]
            self.assertTrue(a.claim(job,now))
            self.assertFalse(b.claim(job,now))
            a.finish(job,now)
            self.assertFalse(b.claim(job,now))
            self.assertFalse(a.claim(jobs[0],now))
            a.close();b.close()

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):due_jobs(dt.datetime(2026,10,16),CONFIG)

    def test_claim_rechecks_lateness_after_slow_preceding_job(self):
        with tempfile.TemporaryDirectory() as folder:
            claims=JobClaims(str(Path(folder)/'jobs.sqlite'))
            now=dt.datetime(2026,10,16,8,0,tzinfo=UK)
            job=due_jobs(now,CONFIG)[-1]
            self.assertFalse(job.missed)
            self.assertFalse(claims.claim(job,now+dt.timedelta(minutes=4)))
            self.assertEqual(claims.db.execute('SELECT status FROM scheduler_jobs').fetchone()[0],'missed')
            claims.close()

    def test_failed_job_is_terminal_and_other_owner_cannot_finish(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'jobs.sqlite');a,b=JobClaims(path),JobClaims(path)
            now=dt.datetime(2026,10,16,8,0,tzinfo=UK);job=due_jobs(now,CONFIG)[-1]
            self.assertTrue(a.claim(job,now))
            b.finish(job,now)
            self.assertEqual(a.db.execute('SELECT status FROM scheduler_jobs').fetchone()[0],'running')
            a.finish(job,now,'Provider outage')
            self.assertFalse(b.claim(job,now+dt.timedelta(seconds=30)))
            a.close();b.close()

if __name__=='__main__':unittest.main()
