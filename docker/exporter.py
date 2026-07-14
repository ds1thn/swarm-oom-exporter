#!/usr/bin/env python3
"""
swarm-oom-exporter
------------------
Опрашивает Docker API (уровень Swarm, через manager-ноду) и считает таски,
завершившиеся предположительно из-за OOM, независимо от того, на какую
ноду Swarm их перепланировал.

Метрики:
  swarm_task_oom_total{service="..."}      - counter, инкрементируется на каждый
                                              обнаруженный OOM-таск
  swarm_oom_exporter_scrape_errors_total   - ошибки опроса Docker API
  swarm_oom_exporter_last_success_timestamp - unix-time последнего успешного опроса
"""

import os
import time
import logging

import docker
from prometheus_client import start_http_server, Counter, Gauge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("swarm-oom-exporter")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9358"))
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "unix://var/run/docker.sock")

# Считаем OOM-подобным только этот exit code (128 + SIGKILL).
# Это эвристика: SIGKILL прилетает и при OOM, и в редких других случаях,
# поэтому дополнительно проверяем DesiredState (см. ниже).
OOM_EXIT_CODE = 137

oom_counter = Counter(
    "swarm_task_oom_total",
    "Number of Swarm tasks killed presumably by OOM",
    ["service"],
)
scrape_errors = Counter(
    "swarm_oom_exporter_scrape_errors_total",
    "Errors while polling the Docker API",
)
last_success = Gauge(
    "swarm_oom_exporter_last_success_timestamp",
    "Unix timestamp of the last successful poll",
)

# ID тасков, которые уже учтены - чтобы не считать их повторно на
# следующих итерациях опроса, пока они ещё видны в API.
_seen_task_ids: set[str] = set()


def get_service_name_map(client: docker.DockerClient) -> dict:
    """service_id -> service_name"""
    try:
        return {s.id: s.attrs["Spec"]["Name"] for s in client.services.list()}
    except docker.errors.APIError:
        log.warning("failed to list services, service names may show as IDs")
        return {}


def poll(client: docker.DockerClient, prime_only: bool = False) -> None:
    """
    prime_only=True: только заполняем _seen_task_ids уже существующими
    завершившимися тасками, ничего не считаем. Нужно на самом первом
    проходе после старта процесса, иначе любой рестарт/редеплой самого
    экспортера трактует ВСЮ историю shutdown-тасков в кластере (в т.ч.
    штатные апдейты/рестарты недельной давности) как свежий OOM.
    """
    global _seen_task_ids

    service_names = get_service_name_map(client)
    tasks = client.api.tasks()

    for task in tasks:
        task_id = task.get("ID")
        if not task_id or task_id in _seen_task_ids:
            continue

        if prime_only:
            status = task.get("Status", {}) or {}
            if status.get("State") == "shutdown":
                _seen_task_ids.add(task_id)
            continue

        status = task.get("Status", {}) or {}
        state = status.get("State")

        # ВАЖНО: настоящий крэш (в т.ч. OOM) переводит таск в State="failed"
        # (в `docker service ps` это видно как "Failed ..." в CURRENT STATE).
        # State="shutdown" - это штатная остановка (redeploy/update/scale
        # down/service rm). Более того, при штатной остановке, если
        # контейнер не уложился в stop_grace_period, Docker сам досылает
        # SIGKILL (тот же exit 137!), но это НЕ OOM - поэтому фильтруем
        # именно по State, а не полагаемся только на ExitCode.
        if state != "failed":
            continue

        # ВАЖНО: не фильтруем по DesiredState. Изначально была идея отличать
        # "OOM с последующим рестартом" (DesiredState=running) от "штатной
        # остановки" (DesiredState=shutdown), но на практике это ломается:
        # если restart-max-attempts исчерпан (или поллер просто не успел
        # между двумя быстрыми рестартами), Swarm выставляет
        # DesiredState=shutdown ДАЖЕ для настоящего краша. Само по себе поле
        # ExitCode=137 уже достаточный сигнал - при штатной остановке
        # (scale down, service update, service rm) ContainerStatus.ExitCode
        # там как правило 0 или отсутствует.
        container_status = status.get("ContainerStatus", {}) or {}
        exit_code = container_status.get("ExitCode")

        if exit_code == OOM_EXIT_CODE:
            service_id = task.get("ServiceID", "")
            service_name = service_names.get(service_id, service_id or "unknown")
            oom_counter.labels(service=service_name).inc()
            log.info(
                "OOM detected: service=%s task=%s exit_code=%s node_id=%s",
                service_name,
                task_id,
                exit_code,
                task.get("NodeID"),
            )

        _seen_task_ids.add(task_id)

    # Простая защита от неограниченного роста множества в памяти.
    if len(_seen_task_ids) > 20000:
        _seen_task_ids = set(list(_seen_task_ids)[-10000:])


def main() -> None:
    client = docker.DockerClient(base_url=DOCKER_SOCK)

    # Быстрая проверка, что мы реально на manager-ноде и Swarm активен.
    try:
        info = client.info()
        swarm_info = info.get("Swarm", {})
        if swarm_info.get("ControlAvailable") is not True:
            log.warning(
                "ControlAvailable=%s - похоже, это не manager-нода или Swarm не активен",
                swarm_info.get("ControlAvailable"),
            )
    except docker.errors.APIError:
        log.exception("не удалось получить docker info при старте")

    start_http_server(METRICS_PORT)
    log.info("swarm-oom-exporter запущен, слушаю :%d/metrics (poll interval=%ds)",
              METRICS_PORT, POLL_INTERVAL)

    # Прогрев: помечаем всё, что уже завершилось ДО старта процесса, как
    # "уже видели", ничего не считая. Иначе любой рестарт/редеплой самого
    # экспортера сгенерирует ложный шторм по всей исторической очереди
    # shutdown-тасков кластера.
    try:
        poll(client, prime_only=True)
        last_success.set(time.time())
        log.info("прогрев завершён, известно %d уже существующих тасков", len(_seen_task_ids))
    except Exception:
        scrape_errors.inc()
        log.exception("ошибка при прогреве")

    while True:
        try:
            poll(client)
            last_success.set(time.time())
        except Exception:
            scrape_errors.inc()
            log.exception("ошибка при опросе Docker API")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
