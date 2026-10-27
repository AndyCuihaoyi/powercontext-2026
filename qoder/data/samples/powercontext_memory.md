# 把长期阅读偏好交给 PowerContext

## 为什么阅读助手需要记忆

单次问答只需要检索，连续研读才需要记忆。用户会反复表达同一类偏好：用中文回答、关注风险而不是营销叙述、引用必须带页码。把这些偏好塞进每次提示词既冗余又容易遗漏。

PowerContext 提供 scope 粒度的记忆：一个 scope 对应一个持续的研究主题，记忆以条目（entry）形式写入，可检索、可退役、可审计。

## scope 的划分

CiteFlow 只创建一个本地 scope，标题为“CiteFlow 研读记忆”。scope 不按文档划分，因为偏好跨文档复用；也不按会话划分，因为会话结束后偏好仍然有效。

scope ID 持久化在 `runtime/powercontext_scope.json`。首次启动时先按标题查找，找不到才创建，并用固定的幂等键避免重复建 scope。

## 三类记忆

- `preference`：稳定的回答偏好，例如“中文回答、先给结论、保留页码”；
- `insight`：研读中形成的判断，例如“3M 的诉讼风险主要集中在耳塞业务”；
- `progress`：阅读进度，例如“正在阅读《3M 2022 10-K》第 42 页”。

类型会拼上用户标识写入 `kind` 字段（如 `preference@local`），这样同一个 scope 也能服务多个本地用户。

## 写入与召回

写入调用 `POST /v1/memory/remember`，响应里的 `entry_id` 与 `x-powercontext-request-id` 会一并镜像到 seekdb 的 `cf_memories` 表，便于在界面上展示“这条记忆确实由 PowerContext 接收”。

召回调用 `POST /v1/memory/search`，模式为 `fts`。检索结果只有文本和分数，没有 kind，因此应用层用本地镜像表把文本映射回类型，并过滤掉其他用户的条目。

## 上下文准备

`POST /v1/context/prepare` 会把与问题相关的记忆组装成一段可直接注入提示词的上下文。CiteFlow 在技术面板里展示这段内容，用于核对“记忆到底带了什么进去”，而不是黑盒拼接。

## 降级策略

PowerContext 是独立进程，可能没有启动。CiteFlow 的处理是：

1. 健康检查失败时把状态标为 `seekdb-fallback`；
2. 写入仍然落到 `cf_memories`，`backend` 字段记为 `seekdb`；
3. 召回退化为本地 LIKE 匹配；
4. 界面明确显示当前后端，不假装记忆服务在线。

本地优先的工具不应该因为一个可选服务缺失就不可用，但也不能隐藏降级事实。
