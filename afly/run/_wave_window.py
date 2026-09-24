"""WaveTracker — pure wave-completion bookkeeping for the lookahead executor.

Split out of ``executor.py`` so the "is this wave done yet" accounting (which
has nothing to do with dispatch, fetching, or ClickHouse) can be read and
reasoned about on its own. A :class:`~afly.run.planner.Plan`'s waves are
static — known in full before a single job is dispatched — so this is built
once per run from the whole ordered wave list, not grown incrementally.
"""

from __future__ import annotations

from afly.run.planner import ChunkJob


class WaveTracker:
    """Tracks, per wave index, how many of its jobs are still non-terminal.

    Two things read this: ``Executor._advance_front`` (a wave may be
    rebuilt once its count hits zero) and ``Executor._admit_more_waves``
    (how far ahead of the oldest-incomplete wave the lookahead window may
    reach). Dispatch order across waves is deliberately not wave order — see
    ``afly.run.scheduler``'s module docstring — so a *later* wave's count can
    reach zero before an earlier one's; only ``front`` (the oldest
    not-yet-rebuilt wave) is ever acted on, which is what keeps rebuilds
    themselves strictly ordered regardless.
    """

    def __init__(self, waves: list[list[ChunkJob]]) -> None:
        self.waves = waves
        self.job_wave: dict[int, int] = {
            id(job): i for i, wave_jobs in enumerate(waves) for job in wave_jobs
        }
        self.remaining: dict[int, int] = {i: len(wave_jobs) for i, wave_jobs in enumerate(waves)}
        self.front = 0
        self.admitted = 0
        self._terminal_jobs: set[int] = set()

    @property
    def total_waves(self) -> int:
        return len(self.waves)

    def note_terminal(self, job: ChunkJob) -> None:
        """Record that *job*'s fate (success/failed/skipped) is now decided.

        Idempotent per job — a downstream downgrade (e.g. the
        empty-response guard) mutates an already-recorded ``JobResult`` in
        place rather than producing a fresh terminal transition, and a
        failed rebuild re-records every job of the wave as ``"failed"`` even
        though most of them were already terminal — neither should double-
        decrement the wave's count.
        """
        jid = id(job)
        if jid in self._terminal_jobs:
            return
        self._terminal_jobs.add(jid)
        self.remaining[self.job_wave[jid]] -= 1

    def wave_complete(self, index: int) -> bool:
        return self.remaining.get(index, 0) == 0

    def has_room_to_admit(self, window_size: int) -> bool:
        return self.admitted < self.total_waves and (self.admitted - self.front) < window_size

    def next_wave_to_admit(self) -> list[ChunkJob]:
        """The next not-yet-admitted wave's jobs; advances the admission edge."""
        wave_jobs = self.waves[self.admitted]
        self.admitted += 1
        return wave_jobs


__all__ = ["WaveTracker"]
