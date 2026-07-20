<template>
  <div class="mb-4 space-y-2">
    <!-- Always-visible Grok dead-account tools -->
    <div class="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 dark:border-rose-900/40 dark:bg-rose-950/30">
      <div class="text-xs text-rose-800 dark:text-rose-200">
        {{ t('admin.accounts.bulkActions.deadToolsHint') }}
      </div>
      <div class="flex flex-wrap gap-2">
        <button type="button" class="btn btn-secondary btn-sm" @click="$emit('probe-dead')">
          {{ t('admin.accounts.bulkActions.probeDead') }}
        </button>
        <button type="button" class="btn btn-danger btn-sm" @click="$emit('delete-dead')">
          {{ t('admin.accounts.bulkActions.deleteDead') }}
        </button>
      </div>
    </div>

    <div class="flex items-center justify-between rounded-lg bg-primary-50 p-3 dark:bg-primary-900/20">
      <div class="flex flex-wrap items-center gap-2">
        <span v-if="selectedIds.length > 0" class="text-sm font-medium text-primary-900 dark:text-primary-100">
          {{ t('admin.accounts.bulkActions.selected', { count: selectedIds.length }) }}
        </span>
        <span v-else class="text-sm font-medium text-primary-900 dark:text-primary-100">
          {{ t('admin.accounts.bulkEdit.title') }}
        </span>
        <template v-if="selectedIds.length > 0">
          <button
            @click="$emit('select-page')"
            class="text-xs font-medium text-primary-700 hover:text-primary-800 dark:text-primary-300 dark:hover:text-primary-200"
          >
            {{ t('admin.accounts.bulkActions.selectCurrentPage') }}
          </button>
          <span class="text-gray-300 dark:text-primary-800">•</span>
          <button
            @click="$emit('clear')"
            class="text-xs font-medium text-primary-700 hover:text-primary-800 dark:text-primary-300 dark:hover:text-primary-200"
          >
            {{ t('admin.accounts.bulkActions.clear') }}
          </button>
        </template>
      </div>
      <div class="flex flex-wrap gap-2">
        <template v-if="selectedIds.length > 0">
          <button @click="$emit('delete')" class="btn btn-danger btn-sm">{{ t('admin.accounts.bulkActions.delete') }}</button>
          <button @click="$emit('reset-status')" class="btn btn-secondary btn-sm">{{ t('admin.accounts.bulkActions.resetStatus') }}</button>
          <button @click="$emit('refresh-token')" class="btn btn-secondary btn-sm">{{ t('admin.accounts.bulkActions.refreshToken') }}</button>
          <button @click="$emit('probe-upstream-billing')" class="btn btn-secondary btn-sm">{{ t('admin.accounts.bulkActions.probeUpstreamBilling') }}</button>
          <button @click="$emit('toggle-schedulable', true)" class="btn btn-success btn-sm">{{ t('admin.accounts.bulkActions.enableScheduling') }}</button>
          <button @click="$emit('toggle-schedulable', false)" class="btn btn-warning btn-sm">{{ t('admin.accounts.bulkActions.disableScheduling') }}</button>
          <button @click="$emit('edit-selected')" class="btn btn-primary btn-sm">{{ t('admin.accounts.bulkActions.edit') }}</button>
        </template>
        <button @click="$emit('edit-filtered')" class="btn btn-primary btn-sm">
          {{ t('admin.accounts.bulkEdit.submit') }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'

defineProps<{ selectedIds: number[] }>()
defineEmits([
  'delete',
  'edit-selected',
  'edit-filtered',
  'clear',
  'select-page',
  'toggle-schedulable',
  'reset-status',
  'refresh-token',
  'probe-upstream-billing',
  'probe-dead',
  'delete-dead'
])

const { t } = useI18n()
</script>
