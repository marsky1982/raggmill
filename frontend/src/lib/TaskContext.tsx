import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { get } from "../api";

interface TaskView { task_id: string; type: string; status: string }
interface TaskState {
  ids: Record<string, string>;
  tasks: TaskView[];
  setTask: (key: string, id: string | null) => void;
}
const TaskContext = createContext<TaskState | null>(null);

function initialIds(): Record<string, string> {
  const ids: Record<string, string> = {};
  for (let i = 0; i < sessionStorage.length; i++) {
    const key = sessionStorage.key(i);
    if (key?.startsWith("ragas-task:")) ids[key.slice(11)] = sessionStorage.getItem(key)!;
  }
  return ids;
}

/** 挂在页面切换之上：恢复请求与重连轮询不会随页面卸载。 */
export function TaskProvider({ children }: { children: ReactNode }) {
  const [ids, setIds] = useState(initialIds);
  const [tasks, setTasks] = useState<TaskView[]>([]);
  const dismissed = useRef(new Set<string>());
  useEffect(() => {
    let live = true;
    let pending = false;
    const refresh = async () => {
      if (pending) return;
      pending = true;
      try {
        const next = await get<TaskView[]>("/api/tasks");
        if (live) setTasks(next.filter((task) => !dismissed.current.has(task.task_id)));
      } catch { /* 保留最后已知状态，下次轮询重试。 */ }
      finally { pending = false; }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 3000);
    return () => { live = false; window.clearInterval(timer); };
  }, []);
  const setTask = useCallback((key: string, id: string | null) => {
    const old = sessionStorage.getItem(`ragas-task:${key}`);
    if (id) sessionStorage.setItem(`ragas-task:${key}`, id);
    else {
      if (old) dismissed.current.add(old);
      sessionStorage.removeItem(`ragas-task:${key}`);
      setTasks((previous) => previous.filter((task) => task.task_id !== old));
    }
    setIds((previous) => {
      const next = { ...previous };
      if (id) next[key] = id; else delete next[key];
      return next;
    });
  }, []);
  return <TaskContext.Provider value={{ ids, tasks, setTask }}>{children}</TaskContext.Provider>;
}

export function useTaskState() {
  const state = useContext(TaskContext);
  if (!state) throw new Error("TaskProvider 未挂载");
  return state;
}
