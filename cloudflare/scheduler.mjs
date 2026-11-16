export default {
  async scheduled(controller, env) {
    const token = env.GITHUB_DISPATCH_TOKEN?.trim();
    if (!token) {
      throw new Error("Configure o secret GITHUB_DISPATCH_TOKEN.");
    }

    const url = `https://api.github.com/repos/${env.GITHUB_REPOSITORY}/actions/workflows/backups.yml/dispatches`;
    const response = await fetch(url, {
      method: "POST",
      redirect: "error",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "User-Agent": "db-backups-cron",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: env.GITHUB_REF }),
    });

    if (!response.ok) {
      throw new Error(`GitHub recusou o disparo de backups (HTTP ${response.status}).`);
    }

    console.log("Workflow de backup disparado.", {
      scheduledTime: new Date(controller.scheduledTime).toISOString(),
      status: response.status,
    });
  },
};
