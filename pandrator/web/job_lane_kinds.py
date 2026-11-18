"""Job kinds that the worker lanes and the session fence treat specially.

Both sets live in this leaf module so the worker loop, the fence in
``JobQueue`` and the work projection read one definition. ``job_handler_domains``
re-exports them as the domain registration's public names.
"""

# Jobs a person waits on briefly. Each worker process also runs a lane that
# claims only these, so an export never queues behind a long generation in
# another project. A subtitle-evidence request belongs here because it is a
# few seconds of re-transcription that is useless once the answer is stale: it
# must not wait behind the render it is meant to support.
# PANDRATOR_DELIVERY_LANE=0 turns the lane off.
DELIVERY_LANE_JOB_KINDS = frozenset({
    "export.create",
    "export.variant",
    "export.comparison",
    "output.mix_preview",
    "session.bundle.export",
    "project.exports.bundle",
    "subtitle.evidence",
})

# Kinds that may run beside a different job in their own session. The session
# fence in ``JobQueue._next_claimable_job_id`` keeps every other queued job
# behind a running one in its session; these jobs only wait for another job of
# the same set, and their own resource keys still have to be free. Evidence is
# read-only beside a render: it registers its own audio clip and transcript
# (never a singleton session role) and reads the subtitle revision and media it
# was pinned to, so a running render cannot invalidate its input.
SESSION_BESIDE_JOB_KINDS = frozenset({"subtitle.evidence"})
