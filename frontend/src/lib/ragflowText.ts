/** RAGFlow 相关文案与地址比较（纯逻辑，供配置页 / KG 页面板与测试共用）。 */
import type { RagflowProbeResult } from "../types";

/** 比较地址时忽略首尾空白与末尾斜杠 */
export const normBaseUrl = (value: string) => value.trim().replace(/\/+$/, "");

/** 拿不到版本时的默认文案：不显示空白，避免让人以为界面坏了 */
export const VERSION_UNKNOWN = "版本未知";

/** RAGFlow 版本展示：接口返回什么就显示什么（不补 v、不截断），取不到时用 missing 文案代替空白 */
export function ragflowVersionText(
  version: string | null | undefined,
  missing: string = VERSION_UNKNOWN,
): string {
  const raw = typeof version === "string" ? version.trim() : "";
  return raw ? `RAGFlow ${raw}` : missing;
}

/** §8.1 六类错误文案；error_code 由 api.ts 挂在 Error 上 */
export function errorText(error: unknown, baseUrl: string): string {
  const code = (error as { errorCode?: string }).errorCode;
  if (code === "upstream_unreachable") {
    return `连不上 RAGFlow：${baseUrl}。请确认地址能从工作台所在机器打开`;
  }
  if (code === "upstream_auth_failed") {
    return "这个 API Key 无效，或它没有该知识库的权限。请在 RAGFlow 右上角头像→配置→API 里核对";
  }
  if (code === "dataset_not_found") {
    return "该连接里找不到这个知识库，可能 ID 填错了。请从上面的列表重新选一个";
  }
  if (code === "upstream_not_json") {
    return "服务返回的不是 RAGFlow 数据（可能被网关或登录页拦截）。请检查反向代理是否放行 /api/v1";
  }
  if (code === "upstream_error") {
    // 上游 HTTP 本身失败（4xx/5xx）：判序在 JSON / 业务码之前，所以 404 这类不会落到 not_json
    const status = (error as { upstreamStatus?: number | null }).upstreamStatus;
    const at = typeof status === "number" ? `（HTTP ${status}）` : "";
    return `RAGFlow 返回了错误响应${at}：接口可能不存在或参数不对。`
      + "请确认地址是否少写/多写了路径前缀（RAGFlow 默认在 /api/v1），或对端版本不受支持；详细响应见后端日志";
  }
  return String(error);
}

/** 文档级图谱从哪来：只开 GraphRAG 开关不会出图，用户会一直「等编译跑完」。
 *  v0.27.1：给文档挂「知识编译模板组」再重新解析；1.0（rc3）：内置流水线都不含知识编译，要改用带
 *  Compiler 节点的摄取流水线解析（直接往文档里挂模板组会被 rc3 静默丢弃）。细节见 FAQ 11.1。 */
export const GRAPH_SOURCE_HINT =
  "注意：只打开 GraphRAG 开关不会生成图谱。RAGFlow v0.27.1 要给文档挂「知识编译模板组」（类型为知识图谱）再重新解析；"
  + "RAGFlow 1.0 要改用带「知识编译（Compiler）」节点的摄取流水线来解析";

/** probe verdict → 底部一句处理建议（§8.1 原口径） */
export function verdictAdvice(result: RagflowProbeResult, baseUrl: string): string {
  // 细分原因优先：「看不到这个库」不是 key 失效，也不是接口不兼容
  if (result.error_code === "dataset_not_found") {
    return "这个连接能列出知识库，但看不到该知识库：可能 ID 填错了，或这把 API Key 没有它的访问权限。请从上面的列表重新选一个";
  }
  switch (result.verdict) {
    case "unreachable":
      return `连不上 RAGFlow：${baseUrl}。请确认地址能从工作台所在机器打开`;
    case "auth_failed":
      return "这个 API Key 无效，或它没有该知识库的权限。请在 RAGFlow 右上角头像→配置→API 里核对";
    case "incompatible":
      return "服务返回的不是 RAGFlow 数据（可能被网关或登录页拦截）。请检查反向代理是否放行 /api/v1";
    case "graph_empty":
      return "该账号在此实例下没有可见知识库，或库里的文档还没有图谱。请回 RAGFlow 建库、上传文档，"
        + "或换一个有权限的 API Key。" + GRAPH_SOURCE_HINT;
    case "partial": {
      // N17：partial 有三种来源，分别给建议（旧文案一律说「还没编译完」）
      const total = result.counts?.documents;
      const unfinished = result.counts?.unfinished_documents ?? 0;
      if (result.stopped_reason) {
        return "检查在请求上限内提前停止，结果只覆盖了一部分文档。可以稍后再检查一次，或直接同步（同步会读取全部文档）";
      }
      if (unfinished > 0) {
        return `有 ${unfinished} 篇文档还没解析完${total ? `（该库共 ${total} 篇）` : ""}：等 RAGFlow 解析结束再检查一次。`
          + `先同步已完成的文档也可以，补齐后再点一次同步即可。${GRAPH_SOURCE_HINT}`;
      }
      return "文档都已解析完，但有的文档没有图谱，或部分实体缺少来源分块：这些文档出题时会缺素材。"
        + `先同步也可以；要补齐图谱，请检查这些文档是否走了带知识编译的流程。${GRAPH_SOURCE_HINT}`;
    }
    case "ready":
      return "7 项检查全部通过，可以直接同步并在下一步生成测试集";
    default:
      return "请按上面失败的阶段回 RAGFlow 排查后重试";
  }
}
