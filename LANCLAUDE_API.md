# LanClaude 移动端 API

## 认证

设置环境变量 `OMBRE_APP_TOKEN`，请求头：

```
Authorization: Bearer <OMBRE_APP_TOKEN>
```

或与 Dashboard 相同：`POST /auth/login` + Cookie。

## 工具 REST

| 方法 | 路径 | Body 示例 |
|------|------|-----------|
| POST | `/api/breath` | `{"mode":"recent","query":""}` |
| POST | `/api/hold` | `{"content":"...","memory_kind":"task"}` |
| POST | `/api/grow` | `{"content":"长文本"}` |
| POST | `/api/trace` | `{"bucket_id":"xxx","task_status":"done"}` |
| POST | `/api/dream` | `{}` |
| POST | `/api/pulse` | `{"include_archive":false}` |

响应：`{"ok":true,"result":"..."}`

## 读取

- `GET /api/timeline?limit=50`
- `GET /api/diary?date=YYYY-MM-DD`
- `GET /api/tasks?status=open`
- `GET /api/about-me?importance_min=7`
- `PATCH /api/tasks/{id}` `{"status":"done"}`
