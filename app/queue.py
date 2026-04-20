from redis import Redis
from rq import Queue

from app.config import settings

redis_conn = Redis.from_url(settings.redis_url)

domains_q = Queue("domains", connection=redis_conn)
content_q = Queue("content", connection=redis_conn)
publish_q = Queue("publish", connection=redis_conn)
