# jukuan-jupyter-forwarder

## 项目背景

聚宽提供了基于浏览器访问的 Jupyter 研究环境，但本地 IDE、脚本工具或自定义客户端在接入时，往往无法直接复用聚宽站点的登录态、Cookie 和请求路径规则。  
这个项目的目标，就是在本地提供一个轻量代理层，把本地发出的 HTTP / WebSocket 请求转发到聚宽的 Jupyter 服务，并处理登录、Cookie 复用和会话续期。

## 项目作用

这个代理主要解决以下问题：

- 让本地工具通过一个固定的本地地址访问聚宽 Jupyter，而不必直接处理聚宽站点的鉴权细节。
- 自动复用本地缓存的 Cookie，在会话失效时尝试重新登录，减少手工维护登录态的成本。
- 统一处理聚宽 Jupyter 的路径映射、请求头透传和 XSRF 相关头信息。
- 为后续扩展本地开发联调、IDE 接入、自动化脚本调用提供一个稳定入口。

## 工作方式

服务启动后，本地会监听 `http://127.0.0.1:8888`：

- HTTP 请求由 `src/proxy.py` 转发到聚宽用户空间下的 Jupyter 路径。
- WebSocket 请求同样通过代理转发，满足 Jupyter 前端对实时通道的依赖。
- `src/auth.py` 负责检查登录状态、执行登录流程，并将 Cookie 持久化到本地 `cookies.json`。
- 当聚宽登录状态检查出现瞬时网络异常时，代理会优先保留当前 Cookie，避免误判为会话失效并频繁重登。

## VS Code 正确用法

推荐使用 VS Code Remote SSH 连接云服务器后，再在远程 VS Code 窗口里连接 Jupyter：

1. 先在云服务器上启动代理服务：

   ```bash
   python src/proxy.py
   ```

2. 在本地 VS Code 中使用 Remote SSH 连接这台云服务器。
3. 在远程 VS Code 窗口中选择 Python 内核 / Existing Jupyter Server。
4. Jupyter Server 地址填写：

   ```text
   http://127.0.0.1:8888
   ```

当前推荐配置下无需填写 token、password 或其他认证信息。`8888` 端口只监听云服务器本机的 `127.0.0.1`，不直接暴露到公网；通过 VS Code Remote 模式访问时，请求发生在远程服务器内部，因此既能连接代理，也避免开放额外公网端口。

## 适用场景

- 本地 IDE 连接聚宽 Jupyter。
- 本地脚本或工具需要访问聚宽 Notebook / 文件树接口。
- 调试聚宽鉴权、Cookie 持久化与代理转发逻辑。

## 当前边界

- 当前实现仍依赖聚宽账号密码，默认配置在 `src/config.py` 中，后续更适合迁移到环境变量。
- `cookies.json` 仅用于本地调试，不应提交真实会话数据。
- 目前测试以单元测试和脚本验证为主，尚未覆盖真实联机场景下的完整回归。
