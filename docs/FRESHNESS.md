# Keeping the timetable from rotting

Written 2026-10-25, answering "if we pull the full timetable, does it go stale, and
how do we keep it current?"

Short answer: **yes, everything goes stale, but at five very different speeds, and
you only have to keep current what is being asked about.** Treating railway data as
one thing is what makes people build a nightly full crawl, which is both useless
and impossible on a free tier.

## The volatility ladder

| Tier | What | Changes | How we keep it | Cost |
|---|---|---|---|---|
| Live | position, delay, platform | seconds | per request, 60 s TTL | 1 call/60 s per train, only while watched |
| Availability | seats, waitlist, RAC | minutes | 15 min TTL, finalists only | paid plan today, so absent |
| Operational | does it run today, diversions, special trains | daily | run-days in the DB, exceptions table not built yet | free |
| Schedule | times, halts, distances, run days | a few times a year, at revisions | rolling refresh keyed on age, hashed for change detection | 1 call per train |
| Static | station names, codes, coordinates | years | refreshed with the schedule sweep | free, comes in the same payload |

The only tier that costs real money is live and availability, and both are already
scoped to "only while someone is asking".

## What is implemented now

**A timestamp on every schedule.** `trains.fetched_at` records when the vendor was
actually asked. Databases created before this existed are migrated in place
(`ALTER TABLE`, not a rebuild), because refusing to open a demo machine's database
would be worse than adding a column.

**A content hash per schedule.** `trains.content_hash` fingerprints name, type, run
days, distance and every stop. On every ingest the repo reports one of three
outcomes:

```
new       the train was not in the database
same      re-fetched and nothing a passenger could notice has changed
changed   something changed; this is the one worth putting in the report
```

Without this, every refresh rewrites every row and a quiet month looks identical to
a broken feed. With it, "5 schedules refreshed, 0 changed" is a real, provable fact.

**A policy with numbers, in one readable file.** `railmind/data/freshness.py`:
schedule 30 days, static 180 days, operational 24 hours. The 30 days is a
compromise, not a measurement: the annual timetable book changes rarely, but halts
and timings move more often, and a month keeps the worst case to a rounding error.
A missing timestamp counts as **infinitely old**, never as fresh.

**A refresh command.**

```bash
python scripts/refresh_timetable.py                          # dry run: what is due
python scripts/refresh_timetable.py --apply --limit 5         # 5 requests
python scripts/refresh_timetable.py --corridor CSMT PUNE --apply
```

It works oldest-first, stops when the vendor refuses more requests, and prints
`new / changed / same / failed` per train. A full 9,300-train crawl is 4-6 hours of
network; refreshing the five trains actually on the demo corridor is five requests.

**Freshness in `/health`.** A `timetable_schedule` dependency reports `ok`,
`degraded` (past policy) or `down`. Verified live: `degraded` when the five seeded
schedules had no timestamp, `ok` once they were reseeded with their true capture
time.

## What is deliberately not built, and why

- **No nightly full crawl.** 100 requests a month makes it impossible, and it would
  be pointless: re-fetching 9,300 unchanged schedules learns nothing the hash would
  not have told us for free.
- **No automatic refresh inside a request.** A search that silently spends vendor
  quota is a search that can die mid-demo. Freshness is reported by `/health` and
  fixed by an explicit command, so quota is only ever spent by a human decision.
- **No operational-exceptions table yet.** Special trains, cancellations and
  diversions are daily facts and are not modelled. The consequence today: a train
  that is cancelled on the day still appears in results. That is the largest
  remaining accuracy gap in the timetable, and it is next on the list, not solved.

## The honest summary for the report

Scheduled times are the most stable thing in this project and the least worth
crawling twice. Live data is the least stable and the only part worth paying for.
Availability sits between them and is currently blocked by a paid plan. A crawl buys
breadth once; a hash plus a timestamp is what keeps it true afterwards.
