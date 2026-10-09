"""Who a billable call is for, without passing it through every function.

The spend ledger records each external call against a user, a job and a call
class. The provider seams (`app.services.llm.anthropic`, `app.services.news.exa`)
are many calls deep under the code that knows those things, so rather than
thread three new parameters through every service function, the boundaries set
them here and the seams read them:

* the API sets them per request (`app.api.middleware`), class `interactive`;
* the worker sets them per job, `interactive` for priority-0 jobs (a user is
  waiting) and `background` for the rest.

`contextvars` are per task and copied into tasks spawned from it, so
concurrent requests and concurrent jobs in one process never see each other's
values. Anything that runs outside both boundaries, such as a script, gets the
defaults: no user, no job, and the `background` class, the stricter of the two
for spending, so an unattributed call can never eat the interactive reserve.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum


class CallClass(StrEnum):
    """Whether someone is waiting on a call. Decides which spend share it draws on."""

    INTERACTIVE = "interactive"
    BACKGROUND = "background"


current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)
current_job_id: ContextVar[int | None] = ContextVar("current_job_id", default=None)
current_call_class: ContextVar[CallClass] = ContextVar(
    "current_call_class", default=CallClass.BACKGROUND
)


@dataclass(frozen=True, slots=True)
class Attribution:
    user_id: int | None
    job_id: int | None
    call_class: CallClass


def current() -> Attribution:
    return Attribution(
        user_id=current_user_id.get(),
        job_id=current_job_id.get(),
        call_class=current_call_class.get(),
    )


@contextmanager
def attributed(
    *,
    call_class: CallClass,
    user_id: int | None = None,
    job_id: int | None = None,
) -> Iterator[None]:
    """Set all three for the duration of the block, then restore the previous values."""
    tokens = (
        current_call_class.set(call_class),
        current_user_id.set(user_id),
        current_job_id.set(job_id),
    )
    try:
        yield
    finally:
        current_call_class.reset(tokens[0])
        current_user_id.reset(tokens[1])
        current_job_id.reset(tokens[2])
