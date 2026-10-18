type Config = { basePath: string; apiBase: string; language: string };

function read(): Config {
  const el = document.getElementById("gozar-config");
  if (el?.textContent) return JSON.parse(el.textContent) as Config;
  return { basePath: "/gozar", apiBase: "/gozar/api", language: "fa" };
}

export const config = read();
