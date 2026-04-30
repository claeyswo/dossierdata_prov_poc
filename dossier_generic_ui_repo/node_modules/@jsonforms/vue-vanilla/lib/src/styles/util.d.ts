import { Styles } from './styles';
export declare const classes: (strings: TemplateStringsArray, ...variables: any[]) => string;
/**
 * Helper function to merge two styles definitions. The contained classes will be combined, not overwritten.
 *
 * Example usage:
 * ```ts
 * const myStyles = mergeStyles(defaultStyles, { control: { root: 'mycontrol' } });
 * ```
 */
export declare const mergeStyles: (stylesA: Partial<Styles>, stylesB: Partial<Styles>) => Partial<Styles>;
