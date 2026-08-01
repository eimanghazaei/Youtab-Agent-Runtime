import type { Meta, StoryObj } from '@storybook/react-vite'

import { Button } from '../button'
import { EyeIcon } from '../icons'
import { Modal } from '../modal'

const meta: Meta<typeof Modal> = {
  component: Modal,
  title: 'Components/Modal'
}

export default meta

type Story = StoryObj<typeof Modal>

export const Default: Story = {
  render: () => (
    <Modal
      trigger={({ open }) => (
        <Button onClick={open} prefix={<EyeIcon />}>
          Open Modal
        </Button>
      )}
    >
      <h2>Modal Content</h2>
      <p>Click outside or press Escape to close.</p>
    </Modal>
  )
}

export const WithBody: Story = {
  render: () => (
    <Modal
      trigger={({ open }) => <Button onClick={open}>Open with body</Button>}
    >
      <h2>A bigger modal</h2>

      <p>
        Native <code>&lt;dialog&gt;</code> under the hood. It portals into{' '}
        <code>document.body</code>, so stacking context isn&apos;t a concern.
      </p>

      <p>Press Escape or click the backdrop to dismiss.</p>
    </Modal>
  )
}
