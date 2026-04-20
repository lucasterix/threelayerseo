from rq import Worker

from app.queue import content_q, domains_q, publish_q, redis_conn


def main() -> None:
    worker = Worker([domains_q, content_q, publish_q], connection=redis_conn)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
