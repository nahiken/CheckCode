# agent
"""ReAct Agent Loop —— 自研核心。

- types.py             核心数据模型(消息/工具调用/流式事件)
- config.py            LLM 配置加载(OpenAI 兼容 / DeepSeek)
- client.py            OpenAI 兼容流式客户端(SSE 增量解析)
- stream_collector.py  流式 Tool Use 事件聚合(文本/思考/工具参数)
- tool_calls.py        并发安全工具分组 + 按模型顺序回灌
- loop.py              ReAct 主循环
"""
