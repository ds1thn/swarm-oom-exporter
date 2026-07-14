# swarm-oom-exporter
```
groups:
  - name: swarm-oom
    rules:
      - alert: SwarmServiceOOMKilled
        expr: increase(swarm_task_oom_total[15m]) > 0
        for: 0m
        labels:
          severity: warning
        annotations:
          summary: "Сервис {{ .service }} был OOM-killed"
          description: "{{  }} OOM-килл(ов) за последние 15 минут"

      - alert: SwarmServiceOOMCrashLoop
        expr: increase(swarm_task_oom_total[30m]) >= 3
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "Сервис {{ .service }} в OOM crash loop"
          description: "{{  }} OOM-киллов за 30 минут - контейнер не успевает жить, вероятно лимит памяти занижен либо утечка"
```
