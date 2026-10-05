# Eval results

**Not run.** The public seed document is not in `ready` status, so there is nothing to
evaluate against yet (status=failed, error=Failed after repeated retries: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. \n* Quota exceeded for metric: generativelanguage.googleapis.com/embed_content_free_tier_requests, limit: 1000, model: gemini-embedding-2\nPlease retry in 18h37m48.87188).

This is most likely the free-tier Gemini quota being exhausted for the day - start the
app (it retries ingestion through the normal job queue) once quota resets and re-run
`python -m eval.run_eval`.
