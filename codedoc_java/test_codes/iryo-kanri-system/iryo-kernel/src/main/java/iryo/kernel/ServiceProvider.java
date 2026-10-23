package iryo.kernel;

import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationTargetException;
import java.util.ArrayList;
import java.util.List;

/**
 * 登録済みサービスを生成するプロバイダ。
 *
 * <p>単一解決は最後の登録を採用し、複数解決は登録順を維持する。
 * 実装型は単一の public コンストラクタを持つものとして、その引数も
 * コンテナから再帰的に解決する。</p>
 */
public final class ServiceProvider {
    private final List<Registration> registrations;

    ServiceProvider(List<Registration> registrations) {
        this.registrations = registrations;
    }

    public <T> T get(Class<T> service) {
        for (int index = registrations.size() - 1; index >= 0; index--) {
            Registration registration = registrations.get(index);
            if (registration.service().equals(service)) {
                return service.cast(resolve(registration));
            }
        }
        throw new IllegalStateException("サービスが登録されていません: " + service.getName());
    }

    public <T> List<T> getAll(Class<T> service) {
        List<T> values = new ArrayList<>();
        for (Registration registration : registrations) {
            if (registration.service().equals(service)) {
                values.add(service.cast(resolve(registration)));
            }
        }
        return List.copyOf(values);
    }

    private Object resolve(Registration registration) {
        if (registration.lifetime() != Lifetime.TRANSIENT && registration.initialized()) {
            return registration.cached();
        }

        Object value = registration.factory() != null
                ? registration.factory().get()
                : instantiate(registration.implementation());

        if (registration.lifetime() != Lifetime.TRANSIENT) {
            registration.cache(value);
        }
        return value;
    }

    private Object instantiate(Class<?> implementation) {
        Constructor<?>[] constructors = implementation.getConstructors();
        if (constructors.length != 1) {
            throw new IllegalStateException(
                    "public コンストラクタは 1 つだけ必要です: " + implementation.getName());
        }

        Constructor<?> constructor = constructors[0];
        Class<?>[] parameterTypes = constructor.getParameterTypes();
        Object[] arguments = new Object[parameterTypes.length];
        for (int index = 0; index < parameterTypes.length; index++) {
            arguments[index] = get(parameterTypes[index]);
        }

        try {
            return constructor.newInstance(arguments);
        } catch (InstantiationException | IllegalAccessException | InvocationTargetException error) {
            throw new IllegalStateException("サービスを生成できません: " + implementation.getName(), error);
        }
    }
}
