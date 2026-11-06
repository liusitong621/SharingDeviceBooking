"""存活探针的回归测试。

为什么单独一个文件：/healthz 是部署后唯一不鉴权的入口，也是巡检脚本的判定依据。
它一旦被改成需要登录，沙盒健康检查会拿到 302，看起来像「服务没起来」；
一旦有人顺手往里加用户数、预约数，公开链接就多了一条信息泄露路径。
两者都不会被业务测试发现，只能在这里钉住。

用 ASGITransport 直连 app 而不跑 lifespan：探针不该依赖数据库已初始化，
测试也不该碰本地 data/chat.db。
"""

from __future__ import annotations

import asyncio
import unittest

import httpx

from app.main import app


class HealthzTest(unittest.TestCase):
    def _get(self, path: str = "/api/healthz"):
        async def call():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                return await c.get(path)

        return asyncio.run(call())

    def test_public_without_cookie(self):
        """不带任何会话凭据也必须 200——沙盒探针不会登录。"""
        resp = self._get()
        self.assertEqual(resp.status_code, 200)

    def test_both_paths_alive(self):
        """/api/healthz 是托管平台的入口，/healthz 是自有服务器与脚本的入口。

        托管平台会占用根路径的 /healthz 返回它自己的探活结果，应用那条路由在
        外部访问不到；两条都保留，才不至于换一个部署形态就丢掉巡检手段。
        """
        for path in ("/api/healthz", "/healthz"):
            with self.subTest(path=path):
                resp = self._get(path)
                self.assertEqual(resp.status_code, 200)
                # CI 是全新 checkout，data/ 被 gitignore，库不存在时探针必须
                # 仍是 200 且报 "starting"——冷启动不等于服务挂了。
                self.assertIn(resp.json()["status"], ("ok", "starting"))

    def test_shape_and_no_business_data(self):
        body = self._get().json()
        self.assertIn(body["status"], ("ok", "starting"))
        self.assertIn("version", body)
        self.assertIn(body["db"], ("ok", "missing"))
        self.assertIn(body["rag"], ("ready", "degraded"))
        self.assertIn(body["llm"], ("configured", "fallback"))
        # status 与 db 必须自洽：库在就是 ok，库不在就是 starting，不能各说各话
        expect = "ok" if body["db"] == "ok" else "starting"
        self.assertEqual(body["status"], expect)
        # 公开链接下，探针只准回报可用性与降级档位
        for leaked in ("users", "bookings", "messages", "emp_no", "token", "secret"):
            self.assertNotIn(leaked, body)


if __name__ == "__main__":
    unittest.main()
