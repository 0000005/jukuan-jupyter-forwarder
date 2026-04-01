# Repository Guidelines

## 项目结构与模块组织
仓库当前只有一个核心目录 `src/`，用于实现聚宽 Jupyter 的代理层。

- `src/proxy.py`：FastAPI 入口，负责 HTTP 与 WebSocket 转发。
- `src/auth.py`：登录、Cookie 校验与本地持久化逻辑。
- `src/config.py`：聚宽账号、代理监听地址等基础配置。
- `src/verify.py`、`src/test_login.py`：联通性与登录流程验证脚本。
- `src/cookies.json`：本地持久化 Cookie；不要提交真实会话数据。

新增代码时，优先放在 `src/` 下，并按“鉴权 / 代理 / 测试脚本”拆分，避免把网络请求、配置和路由逻辑写进同一个文件。

## 构建、测试与开发命令
项目目前是最小化 Python 服务，没有打包流程。常用命令：

- `python src/proxy.py`：启动本地代理服务。
- `python src/verify.py`：检查聚宽站点连通性并验证鉴权流程。
- `python src/test_login.py`：单独测试登录接口与 Cookie 获取。

如果后续补充依赖文件，建议统一使用虚拟环境，并在提交前至少跑一遍验证脚本。

## 编码风格与命名约定
使用 Python 3，缩进为 4 个空格，函数与变量使用 `snake_case`，类名使用 `PascalCase`。异步逻辑统一使用 `async def`，不要混入阻塞式网络请求。配置项集中放在 `config.py`，避免在业务代码中硬编码 URL、端口或账号字段。

## 测试约定
当前仓库以脚本验证为主，尚未引入 `pytest`。新增测试文件时，推荐命名为 `test_*.py`，并覆盖以下场景：Cookie 复用、Cookie 失效后重新登录、代理转发失败时的返回行为。涉及真实账号时，优先保留可重复执行的最小测试，不要把敏感响应写入日志。

## 提交与合并请求要求
仓库目前未初始化 Git 历史，因此没有现成提交规范。建议从现在开始使用简洁的祈使句提交信息，例如：`add cookie reuse check`、`fix websocket proxy path`。提交 PR 时应包含变更目的、验证方式、风险点；如果修改了登录或代理行为，附上本地验证命令与结果摘要。

## 安全与配置提示
账号、密码、Cookie 都属于敏感信息。`cookies.json` 应仅作本地调试使用，避免提交到远端仓库。后续如果继续开发，优先把凭据迁移到环境变量或本地未跟踪配置文件中。
