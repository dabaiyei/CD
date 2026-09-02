import { createApp } from "vue";

import App from "@/App.vue";
import { router } from "@/router";
import { pinia } from "@/stores";
import { useAuthStore } from "@/stores/auth";
import { motionDirective } from "@/lib/motion";
import { initializeTheme } from "@/lib/theme";
import { initializePwaInstall } from "@/lib/pwa";
import "@/styles.css";
import "@/studio-ui.css";
import "@/visual-upgrade.css";
import "@/light-theme.css";
import "@/transitions.css";

initializeTheme();
initializePwaInstall();

for (const eventName of ["gesturestart", "gesturechange", "gestureend"]) {
  document.addEventListener(eventName, (event) => event.preventDefault(), { passive: false });
}

window.addEventListener("cineforge:auth-expired", () => {
  const auth = useAuthStore(pinia);
  auth.clearSession();
  if (router.currentRoute.value.name !== "login") {
    void router.replace({ name: "login", query: { reason: "expired" } });
  }
});

createApp(App)
  .directive("motion", motionDirective)
  .use(pinia)
  .use(router)
  .mount("#app");
