"""业务路由汇总：采集处理路由先于通用资源路由注册。"""
from __future__ import annotations

from app.routers import collection, resource

ROUTERS = [collection, resource]
