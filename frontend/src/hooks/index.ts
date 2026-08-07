/**
 * 自定义 React Hooks
 *
 * 用于优化性能、处理副作用和状态管理
 */

import { useEffect, useRef, useCallback, useState } from "react";
import { useQuery, UseQueryOptions, UseQueryResult } from "@tanstack/react-query";

/**
 * 使用防抖值
 *
 * 延迟更新值，避免频繁触发操作（如搜索、API 调用）
 *
 * @param value 原始值
 * @param delay 延迟时间（毫秒）
 * @returns 防抖后的值
 *
 * @example
 * const [searchTerm, setSearchTerm] = useState("");
 * const debouncedSearchTerm = useDebounce(searchTerm, 500);
 *
 * useEffect(() => {
 *   // 只在用户停止输入 500ms 后触发搜索
 *   if (debouncedSearchTerm) {
 *     searchAPI(debouncedSearchTerm);
 *   }
 * }, [debouncedSearchTerm]);
 */
export function useDebounce<T>(value: T, delay: number = 500): T {
  const [debouncedValue, setDebouncedValue] = useState<T>(value);

  useEffect(() => {
    const handler = setTimeout(() => {
      setDebouncedValue(value);
    }, delay);

    return () => {
      clearTimeout(handler);
    };
  }, [value, delay]);

  return debouncedValue;
}

/**
 * 使用节流值
 *
 * 限制值更新频率，避免过度渲染（如滚动、拖拽）
 *
 * @param value 原始值
 * @param interval 节流间隔（毫秒）
 * @returns 节流后的值
 *
 * @example
 * const [scrollY, setScrollY] = useState(0);
 * const throttledScrollY = useThrottle(scrollY, 200);
 *
 * useEffect(() => {
 *   const handleScroll = () => setScrollY(window.scrollY);
 *   window.addEventListener("scroll", handleScroll);
 *   return () => window.removeEventListener("scroll", handleScroll);
 * }, []);
 */
export function useThrottle<T>(value: T, interval: number = 200): T {
  const [throttledValue, setThrottledValue] = useState<T>(value);
  const lastUpdated = useRef<number>(Date.now());

  useEffect(() => {
    const now = Date.now();
    const timeSinceLastUpdate = now - lastUpdated.current;

    if (timeSinceLastUpdate >= interval) {
      setThrottledValue(value);
      lastUpdated.current = now;
    } else {
      const handler = setTimeout(() => {
        setThrottledValue(value);
        lastUpdated.current = Date.now();
      }, interval - timeSinceLastUpdate);

      return () => {
        clearTimeout(handler);
      };
    }
  }, [value, interval]);

  return throttledValue;
}

/**
 * 使用上一次的值
 *
 * 保存上一次渲染的值，用于比较或撤销操作
 *
 * @param value 当前值
 * @returns 上一次的值
 *
 * @example
 * const [count, setCount] = useState(0);
 * const prevCount = usePrevious(count);
 *
 * console.log(`从 ${prevCount} 变为 ${count}`);
 */
export function usePrevious<T>(value: T): T | undefined {
  const ref = useRef<T | undefined>(undefined);

  useEffect(() => {
    ref.current = value;
  }, [value]);

  return ref.current;
}

/**
 * 使用挂载状态
 *
 * 检查组件是否已挂载，避免在卸载后的组件上调用 setState
 *
 * @returns 是否已挂载
 *
 * @example
 * const isMounted = useIsMounted();
 *
 * async function fetchData() {
 *   const data = await api.getData();
 *   if (isMounted()) {
 *     setData(data);
 *   }
 * }
 */
export function useIsMounted(): () => boolean {
  const isMounted = useRef(false);

  useEffect(() => {
    isMounted.current = true;
    return () => {
      isMounted.current = false;
    };
  }, []);

  return useCallback(() => isMounted.current, []);
}

/**
 * 使用可取消的查询
 *
 * 自动取消未完成的请求，避免内存泄漏和竞态条件
 *
 * @param queryKey 查询键
 * @param queryFn 查询函数
 * @param options 查询选项
 * @returns 查询结果
 *
 * @example
 * const { data, isLoading } = useCancellableQuery(
 *   ["shots", chapterId],
 *   ({ signal }) => api.shots.list(chapterId, { signal }),
 *   { enabled: !!chapterId }
 * );
 */
export function useCancellableQuery<T>(
  queryKey: unknown[],
  queryFn: (context: { signal: AbortSignal }) => Promise<T>,
  options?: Omit<UseQueryOptions<T>, "queryKey" | "queryFn">
): UseQueryResult<T> {
  return useQuery({
    queryKey,
    queryFn: ({ signal }) => queryFn({ signal }),
    ...options,
  });
}

/**
 * 使用局部存储状态
 *
 * 将状态同步到 localStorage，刷新后保留
 *
 * @param key 存储键
 * @param initialValue 初始值
 * @returns [值, 设置值函数]
 *
 * @example
 * const [collapsed, setCollapsed] = useLocalStorage("sidebar-collapsed", false);
 */
export function useLocalStorage<T>(
  key: string,
  initialValue: T
): [T, (value: T | ((prev: T) => T)) => void] {
  const [storedValue, setStoredValue] = useState<T>(() => {
    try {
      const item = window.localStorage.getItem(key);
      return item ? JSON.parse(item) : initialValue;
    } catch (error) {
      console.warn(`Error reading localStorage key "${key}":`, error);
      return initialValue;
    }
  });

  const setValue = useCallback(
    (value: T | ((prev: T) => T)) => {
      try {
        const valueToStore = value instanceof Function ? value(storedValue) : value;
        setStoredValue(valueToStore);
        window.localStorage.setItem(key, JSON.stringify(valueToStore));
      } catch (error) {
        console.warn(`Error setting localStorage key "${key}":`, error);
      }
    },
    [key, storedValue]
  );

  return [storedValue, setValue];
}

/**
 * 使用窗口尺寸
 *
 * 响应式获取窗口宽高，自动更新
 *
 * @returns { width, height }
 *
 * @example
 * const { width, height } = useWindowSize();
 * const isMobile = width < 768;
 */
export function useWindowSize() {
  const [size, setSize] = useState({
    width: window.innerWidth,
    height: window.innerHeight,
  });

  useEffect(() => {
    const handleResize = () => {
      setSize({
        width: window.innerWidth,
        height: window.innerHeight,
      });
    };

    // 使用防抖避免频繁触发
    let timeoutId: number;
    const debouncedResize = () => {
      clearTimeout(timeoutId);
      timeoutId = window.setTimeout(handleResize, 150);
    };

    window.addEventListener("resize", debouncedResize);
    return () => {
      window.removeEventListener("resize", debouncedResize);
      clearTimeout(timeoutId);
    };
  }, []);

  return size;
}

/**
 * 使用媒体查询
 *
 * 监听 CSS 媒体查询状态
 *
 * @param query 媒体查询字符串
 * @returns 是否匹配
 *
 * @example
 * const isMobile = useMediaQuery("(max-width: 768px)");
 * const isDark = useMediaQuery("(prefers-color-scheme: dark)");
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    const mediaQuery = window.matchMedia(query);
    const handler = (event: MediaQueryListEvent) => setMatches(event.matches);

    // 现代浏览器
    if (mediaQuery.addEventListener) {
      mediaQuery.addEventListener("change", handler);
      return () => mediaQuery.removeEventListener("change", handler);
    }
    // 旧浏览器
    else {
      mediaQuery.addListener(handler);
      return () => mediaQuery.removeListener(handler);
    }
  }, [query]);

  return matches;
}

/**
 * 使用懒加载
 *
 * 当元素进入视口时才加载内容（用于图片、组件）
 *
 * @param threshold 触发阈值（0-1）
 * @returns [ref, 是否可见]
 *
 * @example
 * const [ref, isVisible] = useIntersectionObserver(0.1);
 *
 * return (
 *   <div ref={ref}>
 *     {isVisible && <ExpensiveComponent />}
 *   </div>
 * );
 */
export function useIntersectionObserver(
  threshold: number = 0
): [React.RefObject<HTMLDivElement | null>, boolean] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [isVisible, setIsVisible] = useState(false);

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => {
        setIsVisible(entry.isIntersecting);
      },
      { threshold }
    );

    const currentRef = ref.current;
    if (currentRef) {
      observer.observe(currentRef);
    }

    return () => {
      if (currentRef) {
        observer.unobserve(currentRef);
      }
    };
  }, [threshold]);

  return [ref, isVisible];
}
