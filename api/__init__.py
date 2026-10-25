"""WhyCode 浏览器工作台 API(M7)。

**本包刻意不做任何 re-export。**

历史教训(M9 review S2 / `tests/api/test_approvals_api.py`):此前这里有
`from api.app import app`,它把**父包属性** `app` 绑成 FastAPI **实例**,遮蔽了
同名子模块 —— 于是 `import api.app as api_app` 绑到的是实例而非模块,
所有 `monkeypatch.setattr(api_app, ...)` 都打不中真实模块。

不 re-export 还带来一个可组合性收益:子模块(如 `api.tasks`)可被无 FastAPI 依赖的
调用方(命令行工具、测试)直接导入,不会顺带拉起 `api.app` 与其整个 FastAPI 依赖链。

ASGI 入口请直接写 `api.app:app`(Dockerfile 与文档均为此写法,它走「导入子模块再取
属性」,与包属性遮蔽无关)。
"""
