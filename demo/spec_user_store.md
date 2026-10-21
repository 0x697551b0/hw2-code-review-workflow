# 用户存储服务

## 模块
`user_store`

## 功能

- [R1] 创建用户：`create_user(cursor, name, email, age)`
- [R2] 按主键查询用户：`get_user(cursor, user_id)`
- [R3] 按名称关键词搜索用户：`find_users(cursor, keyword, limit_collector)`
- [R4] 加载服务配置：`load_config(path)`

## 数据模型
`users(id, name, email, age)`

## 存储
SQLite。连接由调用方以 `cursor` 传入（具备 `execute(sql, params)`、`fetchone()`、`fetchall()`、
`lastrowid`），模块不自己管理连接与事务，便于被上层服务复用与测试。

## 约束
- 只依赖 Python 标准库；
- 密码等敏感配置必须来自环境变量，不得出现在源码里；
- 所有 SQL 必须参数化。
