"""Optional, fail-closed extraction of rendered React message metadata.

The reader may use this only as corroborating observation evidence.  It does
not make Relay internals, DOM nodes, or a message id durable across remounts.
The mounted-branch check is independently implemented from the public React
Fiber reflection semantics (facebook/react, MIT).
"""

from __future__ import annotations


# This reads own data-property descriptors only: no DOM attributes, application
# getters, stores, or functions are consulted.
RUNTIME_MESSAGE_METADATA_JS = r"""(root) => {
    const data = (object, key) => {
        if (!object || (typeof object !== 'object' && typeof object !== 'function')) return undefined;
        const descriptor = Object.getOwnPropertyDescriptor(object, key);
        return descriptor && Object.prototype.hasOwnProperty.call(descriptor, 'value')
            ? descriptor.value : undefined;
    };
    const ownKeys = object => object && (typeof object === 'object' || typeof object === 'function')
        ? Object.keys(Object.getOwnPropertyDescriptors(object)) : [];
    const one = values => values.length && values.every(value => value === values[0]) ? values[0] : undefined;
    const text = value => typeof value === 'string' && value.length > 0 && value.length <= 512
        ? value : undefined;
    const decimal = value => typeof value === 'string' && /^\d+$/.test(value) ? value : undefined;

    const fiberKeys = ownKeys(root).filter(key => key.startsWith('__reactFiber$'));
    if (fiberKeys.length !== 1) return null;
    const attached = data(root, fiberKeys[0]), alternate = data(attached, 'alternate');
    let resolved = null;
    if (!alternate) {
        // A newly mounted branch has no alternate. Accept it only when every
        // ancestor leads to the root's current branch and no insertion bit is
        // pending (public React Placement=2, Hydrating=4096 flags).
        let node = attached, mounted = attached;
        for (let hops = 0; node && hops < 1024; hops++) {
            const flags = data(node, 'flags'), parent = data(node, 'return');
            if (typeof flags !== 'number') return null;
            if ((flags & 4098) !== 0) mounted = parent;
            if (!parent) {
                if (data(node, 'tag') === 3 && data(data(node, 'stateNode'), 'current') === node
                        && mounted === attached) resolved = attached;
                break;
            }
            // A detached fiber can retain a return pointer. The parent must
            // still own this exact child; otherwise its props are stale.
            let ownsChild = false;
            for (let child = data(parent, 'child'), steps = 0;
                    child && steps < 128; child = data(child, 'sibling'), steps++) {
                if (child === node) { ownsChild = true; break; }
            }
            if (!ownsChild) return null;
            node = parent;
        }
        if (!resolved) return null;
    }
    if (!resolved) {
    // Resolve a current alternate through parent ownership. A whole-root tree
    // walk is wrong after React bailouts, which may share child pointers.
    let left = attached, right = alternate;
    for (let hops = 0; hops < 1024; hops++) {
        const leftParent = data(left, 'return');
        if (!leftParent) {
            if (data(left, 'tag') !== 3) return null;
            const rootCurrent = data(data(left, 'stateNode'), 'current');
            if (rootCurrent === left) resolved = attached;
            else if (rootCurrent === right) resolved = alternate;
            else return null;
            break;
        }
        const rightParent = data(leftParent, 'alternate');
        if (!rightParent || data(left, 'alternate') !== right) return null;
        const leftChild = data(leftParent, 'child'), rightChild = data(rightParent, 'child');
        if (leftChild === rightChild) return null;
        if (data(left, 'return') !== data(right, 'return')) {
            left = leftParent;
            right = rightParent;
            continue;
        }
        // Resolving a shared parent needs a potentially wide sibling scan.
        // That is not justified for this optional observation path.
        return null;
    }
    if (!resolved) return null;
    }

    // A detached alternate may retain return links, so verify the selected
    // branch's exact child ownership before inspecting its props.
    let reachedCurrentRoot = false;
    for (let node = resolved, hops = 0; node && hops < 1024;) {
        const parent = data(node, 'return');
        if (!parent) {
            if (data(node, 'tag') !== 3 || data(data(node, 'stateNode'), 'current') !== node) return null;
            reachedCurrentRoot = true;
            break;
        }
        let ownsChild = false;
        for (let child = data(parent, 'child'), steps = 0;
                child && steps < 128; child = data(child, 'sibling'), steps++) {
            if (child === node) { ownsChild = true; break; }
        }
        if (!ownsChild) return null;
        node = parent;
        hops++;
    }
    if (!reachedCurrentRoot) return null;

    const props = [];
    for (let node = resolved, hops = 0; node && hops < 1024; node = data(node, 'return'), hops++) {
        const value = data(node, 'memoizedProps');
        if (value && typeof value === 'object') props.push(value);
    }
    const values = key => props.map(props => data(props, key)).filter(value => value !== undefined);
    const directValues = values('messageId'), currentValues = values('currentMessageRef');
    const directId = text(one(directValues));
    const currentRef = one(currentValues), currentId = text(data(currentRef, 'id'));
    if (!directId || !currentRef) {
        return directValues.length && currentValues.length ? {runtime_identity_conflict: true} : null;
    }
    if (!currentId) return null;
    if (currentId !== directId) return {runtime_identity_conflict: true};

    const result = {runtime_message_id: directId};
    const directions = values('outgoing');
    const direction = one(directions);
    if (typeof direction === 'boolean') result.runtime_outgoing = direction;
    else if (directions.length) return {...result, runtime_outgoing_conflict: true};

    const slideRef = one(values('slideMessageRef'));
    const timestamp = one([decimal(data(slideRef, 'timestamp_ms')), decimal(data(currentRef, 'timestamp_ms'))]
        .filter(value => value !== undefined));
    if (timestamp && decimal(data(slideRef, 'timestamp_ms')) === timestamp
            && decimal(data(currentRef, 'timestamp_ms')) === timestamp) result.runtime_timestamp_ms = timestamp;
    const sender = one([text(data(slideRef, 'sender_fbid')), text(data(currentRef, 'sender_fbid'))]
        .filter(value => value !== undefined));
    if (sender && text(data(slideRef, 'sender_fbid')) === sender
            && text(data(currentRef, 'sender_fbid')) === sender) result.runtime_sender_fbid = sender;
    return result;
}"""
