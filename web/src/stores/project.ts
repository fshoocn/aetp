import { defineStore } from "pinia";
import { ref } from "vue";
import { aetpApi, type Project } from "@/api/endpoints";

export const useProjectStore = defineStore("project", () => {
  const projects = ref<Project[]>([]);
  const currentProjectId = ref<string | null>(
    localStorage.getItem("current_project_id")
  );

  async function load() {
    projects.value = await aetpApi.projects.list();
    const currentExists = projects.value.some(
      (project) => project.project_id === currentProjectId.value
    );
    if (!currentExists) {
      currentProjectId.value = projects.value[0]?.project_id || null;
      if (currentProjectId.value) {
        localStorage.setItem("current_project_id", currentProjectId.value);
      } else {
        localStorage.removeItem("current_project_id");
      }
    }
    return currentProjectId.value;
  }

  function select(projectId: string) {
    if (!projects.value.some((project) => project.project_id === projectId)) {
      return;
    }
    currentProjectId.value = projectId;
    localStorage.setItem("current_project_id", projectId);
  }

  return { projects, currentProjectId, load, select };
});