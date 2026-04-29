# ST-Relay

这是一个运行在本地的 SillyTavern 中转服务器，用于转发消息。

## 功能
- 转发SillyTavern发送/收到的消息。
- API KEY从SillyTavern获取
- 将 发送 / 接收 的消息写入`message.json`/`response.json`。
- 现在可以在config.ini中调整思考模式开关

## 快速开始
1. 确保已安装 Python 环境。
2. 运行 `pip install -r requirements.txt` 安装必要库。
3. 根据 `config.ini.example`配置运行在本地的端口号以及LLM base URL。
4. 使用 `start.bat` 启动。

## 一些闲话
- 希望明天也是个好天气