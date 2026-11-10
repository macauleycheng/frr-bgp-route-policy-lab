#!/usr/bin/env bash
# 清理 lab:移除容器与 docker network(不移除 image)
cd "$(dirname "$0")" && docker compose down -v --remove-orphans
