import { create } from 'zustand'
import { Pod, FolderGroup, TrainingJob } from '@/types'

interface PodState {
  pods: Pod[]
  selectedPod: Pod | null
  folderGroups: FolderGroup[]
  trainingJobs: TrainingJob[]
  
  // Actions
  setPods: (pods: Pod[]) => void
  setSelectedPod: (pod: Pod | null) => void
  setFolderGroups: (groups: FolderGroup[]) => void
  setTrainingJobs: (jobs: TrainingJob[]) => void
  updatePodStatus: (podId: string, status: Pod['status']) => void
}

export const usePodStore = create<PodState>((set) => ({
  pods: [],
  selectedPod: null,
  folderGroups: [],
  trainingJobs: [],
  
  setPods: (pods) => set({ pods }),
  setSelectedPod: (pod) => set({ selectedPod: pod }),
  setFolderGroups: (groups) => set({ folderGroups: groups }),
  setTrainingJobs: (jobs) => set({ trainingJobs: jobs }),
  
  updatePodStatus: (podId, status) => 
    set((state) => ({
      pods: state.pods.map((pod) =>
        pod.pod_id === podId ? { ...pod, status } : pod
      ),
    })),
}))
