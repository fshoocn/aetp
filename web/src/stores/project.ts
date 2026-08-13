import { defineStore } from "pinia";
import { computed, ref } from "vue";
import { aetpApi, type Project } from "@/api/endpoints";

export const useProjectStore = defineStore("project", () => {
  const projects = ref<Project[]>([]);
  const currentProjectId = ref<string | null>(
    localStorage.getItem("current_project_id")
  );
  const currentRole = ref<Project["project_role"]>(null);
  const currentProject = computed(() =>
    projects.value.find((project) => project.project_id === currentProjectId.value)
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
    currentRole.value = projects.value.find(
      (project) => project.project_id === currentProjectId.value
    )?.project_role ?? null;
    return currentProjectId.value;
  }

  function select(projectId: string) {
    if (!projects.value.some((project) => project.project_id === projectId)) {
      return;
    }
    currentProjectId.value = projectId;
    currentRole.value = projects.value.find(
      (project) => project.project_id === projectId
    )?.project_role ?? null;
    localStorage.setItem("current_project_id", projectId);
  }

  return { projects, currentProjectId, currentProject, currentRole, load, select };
});