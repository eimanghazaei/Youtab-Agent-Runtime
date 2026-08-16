export declare function Modal({ children, className, id, trigger, ...props }: ModalProps): import("react").JSX.Element;
interface ModalProps extends Omit<React.ComponentProps<'dialog'>, 'open'> {
    trigger: (controls: {
        close: () => void;
        open: () => void;
    }) => React.ReactNode;
}
export {};
