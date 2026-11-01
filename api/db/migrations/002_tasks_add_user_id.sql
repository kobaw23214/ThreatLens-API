-- migration to add user_id to tasks
-- version: 002
-- applies to: tasks
--

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id);

COMMENT ON COLUMN tasks.user_id IS '提交任务的用户 ID';
