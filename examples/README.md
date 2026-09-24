# Examples

## Job funnel

Input: `jobs.sample.json`

Run:

```bash
python3 scripts/build_candidate_inventory.py \
  --targets-config config/targets.example.yaml \
  --output /tmp/candidate_inventory.json

python3 scripts/merge_job_sources.py \
  --inputs examples/jobs.sample.json \
  --output /tmp/jobs.json \
  --history /tmp/job_history.json \
  --targets-config config/targets.example.yaml \
  --as-of 2026-08-23

python3 scripts/score_jobs.py \
  --config config/targets.example.yaml \
  --inventory /tmp/candidate_inventory.json \
  --input /tmp/jobs.json \
  --output /tmp/queue.json \
  --all-output /tmp/decisioned.json \
  --as-of 2026-08-23
```

Expected summary: `decision_summary.expected.json`.

## Interview feedback

Input: `interview_debrief.sample.json`.

Expected behavior: two unique questions, one `weak` OS question and one `strong` project/testing question.

## Daily Check-in

Input: `daily_checkin.sample.json`.

At a 50% completion rate, optional modules are removed and remaining study blocks are shortened. Deadline-critical application tasks remain.

## Semantic candidate evidence

`semantic_candidate_evidence.sample.json` demonstrates the generated, anonymized evidence contract. In a real checkout, maintain facts only under `cv/materials/` and run `python3 scripts/build_candidate_evidence.py`; never hand-edit the ignored runtime JSON or add identity/contact data.
