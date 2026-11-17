"""The watch list posts only topics a handler takes (the work queue spec C4): a to-do no handler takes stays
due until the janitor's hour is out, so the queue never reads as drained."""

from types import SimpleNamespace

from app.jobsearch.work.queue import handlers, topics, watch


def _postable() -> dict[str, str]:
    """Every topic any watched model can post, with the model that names it."""
    found: dict[str, str] = {}
    for model, spec in watch.WATCH.items():
        posts = [*spec.created, *spec.deleted, *(p for rule in spec.rules for p in rule.posts)]
        for post in posts:
            names = ([post.topic] if isinstance(post.topic, str)
                     else [post.topic(SimpleNamespace(kind=k)) for k in topics.READY_KINDS])
            for name in names:
                if name:
                    found.setdefault(name, model.__name__)
    return found


def test_every_topic_the_watch_posts_has_a_handler():
    unhandled = {name: model for name, model in _postable().items() if name not in handlers.HANDLERS}
    assert unhandled == {}, "a to-do no handler takes stays due for an hour; add the post with its first handler"
