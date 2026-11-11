import { latticeBackendTimedOut } from './latticeBackend.mjs';

// 模型返回的 JSON 不合约定时，使用者能做的动作只有「再写一次，不行换模型」，
// 具体是哪个字段不合规对他没有区别，因此这七个码共用一句。
// 精确的码仍然原样写回 reason，排查时看得到。
const FORMAT_MISMATCH = '模型这次没按约定格式返回，再写一次通常能过；反复这样请换一个书写模型。';

// 每个码一句能指导下一步动作的话。新增码请在这里显式登记，
// 不要依赖兜底——兜底句只用于真正没预料到的异常。
const MESSAGES = {
  // 没启用
  narrative_writer_disabled: '网页 Writer 还没启用，请在设置里打开后重新预览。',
  narrative_writer_disabled_main_model_authoring: '当前由主模型书写叙事，预览走不通；请在设置里切换到网页 Writer。',
  // 配置缺项
  narrative_writer_command_not_configured: '没有配置本地 Writer 命令，请检查 LATTICE_WRITER_COMMAND。',
  narrative_writer_model_not_configured: '还没有选择书写模型，请在设置里选一个。',
  // 模型侧：由 writerFailureCode 按 HTTP 状态分流产生
  narrative_writer_auth_failed: '书写模型的密钥无效或已过期，请在设置里更新。',
  narrative_writer_rate_limited: '书写模型的额度不足或已触发限流，稍后再试。',
  narrative_writer_model_unavailable: '这个模型不支持当前请求，请换一个书写模型。',
  narrative_writer_upstream_unavailable: '书写模型的上游暂时不可用，稍后再试。',
  // 旧码，保留映射，避免历史批次回放时落到兜底
  narrative_upstream_failed: '书写模型的上游暂时不可用，稍后再试。',
  // 格式不符：七个细分码共用一个出口
  narrative_writer_result_not_object: FORMAT_MISMATCH,
  narrative_writer_result_types_invalid: FORMAT_MISMATCH,
  narrative_writer_result_schema_invalid: FORMAT_MISMATCH,
  narrative_writer_issues_invalid: FORMAT_MISMATCH,
  narrative_writer_review_invalid: FORMAT_MISMATCH,
  narrative_writer_review_schema_invalid: FORMAT_MISMATCH,
  narrative_writer_review_types_invalid: FORMAT_MISMATCH,
  narrative_writer_invalid_output: FORMAT_MISMATCH,
  // 自称够料但交不出正文
  narrative_writer_sufficient_result_invalid: '模型认为材料足够，但没写出正文或自检没通过。补充材料后再写一次。',
  // 超时与进程
  narrative_writer_timeout: '叙事卷生成超过 5 分钟，请重新预览。',
  narrative_runner_failed: '本地 Writer 进程中断或输出超过 2 MB，请查看 runner 日志后重试。',
  // 身份
  narrative_identity_unavailable: '读不到实例姓名与 Writer 配置，请稍后再试。',
};

/**
 * 把预览阶段的异常翻译成状态码、原因码与一句可行动的话。
 * reason 保留最精确的码，message 按「能采取的动作」归组。
 */
export function narrativePreviewFailure(error) {
  const code = String(error?.message || '').trim();
  if (MESSAGES[code]) {
    // 书写超时是网关等待超时，其余都是上游侧问题
    return { statusCode: code === 'narrative_writer_timeout' ? 504 : 502,
      payload: { status: 'error', reason: code, message: MESSAGES[code], writes_performed: [] } };
  }
  if (latticeBackendTimedOut(error)) {
    return { statusCode: 504, payload: { status: 'error', reason: 'narrative_preview_timeout',
      message: '读取材料或模型设置超时，尚未完成书写，请稍后再试。', writes_performed: [] } };
  }
  return { statusCode: 502, payload: { status: 'error', reason: 'narrative_preview_failed',
    message: '书写服务暂不可用，请稍后再试。', writes_performed: [] } };
}
