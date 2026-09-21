export function selectTaskFromSearch(search, tasks) {
  const rawTaskId = new URLSearchParams(search).get("task");
  if (!rawTaskId || !/^\d+$/.test(rawTaskId)) return null;

  const taskId = Number(rawTaskId);
  if (!Number.isSafeInteger(taskId)) return null;

  return tasks.some((task) => task.id === taskId) ? taskId : null;
}
