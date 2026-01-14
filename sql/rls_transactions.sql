-- 为transactions表启用RLS
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;

-- 用户只能看到自己的交易记录
CREATE POLICY "Users can view own transactions"
  ON transactions
  FOR SELECT
  TO authenticated
  USING (user_id = current_user_id());

-- 用户只能插入自己的交易记录
CREATE POLICY "Users can insert own transactions"
  ON transactions
  FOR INSERT
  TO authenticated
  WITH CHECK (user_id = current_user_id());

-- 用户只能更新自己的交易记录
CREATE POLICY "Users can update own transactions"
  ON transactions
  FOR UPDATE
  TO authenticated
  USING (user_id = current_user_id());

-- 用户只能删除自己的交易记录
CREATE POLICY "Users can delete own transactions"
  ON transactions
  FOR DELETE
  TO authenticated
  USING (user_id = current_user_id());

-- 同样为documents表启用RLS
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own documents"
  ON documents FOR SELECT TO authenticated
  USING (user_id = current_user_id());

CREATE POLICY "Users can insert own documents"
  ON documents FOR INSERT TO authenticated
  WITH CHECK (user_id = current_user_id());

-- MF导出批次RLS
ALTER TABLE mf_export_batches ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own exports"
  ON mf_export_batches FOR SELECT TO authenticated
  USING (user_id = current_user_id());