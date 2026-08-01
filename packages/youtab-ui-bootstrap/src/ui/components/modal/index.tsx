'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { cn } from '../../../utils'

export function Modal({
  children,
  className,
  id,
  trigger,
  ...props
}: ModalProps) {
  const ref = useRef<HTMLDialogElement>(null)
  const [mounted, setMounted] = useState(false)

  useEffect(() => setMounted(true), [])

  const open = useCallback(() => ref.current?.showModal(), [])
  const close = useCallback(() => ref.current?.close(), [])

  return (
    <>
      {trigger({ close, open })}

      {mounted &&
        createPortal(
          <dialog
            className={cn('modal', className)}
            onClick={e => e.target === e.currentTarget && close()}
            {...{ id, ref }}
            {...props}
          >
            <div className="modal-body post">{children}</div>
          </dialog>,
          document.body
        )}
    </>
  )
}

interface ModalProps extends Omit<React.ComponentProps<'dialog'>, 'open'> {
  trigger: (controls: {
    close: () => void
    open: () => void
  }) => React.ReactNode
}
