/** Recover one transient module fetch without reloading a page with unsaved edits. */
export async function loadLazyModule<T>(load: () => Promise<T>): Promise<T> {
  try {
    return await load();
  } catch (error) {
    if (
      !(error instanceof TypeError) ||
      !/fetch|import|network|loading.*module|module.*load/i.test(error.message)
    )
      throw error;
    await new Promise<void>((resolve) => setTimeout(resolve, 250));
    return load();
  }
}
