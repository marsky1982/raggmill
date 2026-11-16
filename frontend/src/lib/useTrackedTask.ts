import { useCallback, useEffect } from "react";
import { useTaskState } from "./TaskContext";

/** 后端为权威；sessionStorage仅保存未确认终态的任务ID，不含凭据或内容。 */
export function useTrackedTask(key: string, types: string[]) {
  const { ids, tasks, setTask } = useTaskState();
  const id = ids[key] ?? null;
  const signature = types.join(",");
  useEffect(() => {
    if (id) return;
    const found = tasks.find((task) => signature.split(",").includes(task.type)
      && ["pending", "running", "cancel_requested"].includes(task.status));
    if (found) setTask(key, found.task_id);
  }, [id, key, signature, tasks, setTask]);
  const setId = useCallback((next: string | null) => setTask(key, next), [key, setTask]);
  return [id, setId] as const;
}
