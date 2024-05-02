from Operators import *
import time


# Random Spinmatrix, same for every site
class NonGaugeLinear(nn.Module):
    def __init__(self, input_dof, output_dof):
        super(NonGaugeLinear, self).__init__()
        self.weights = nn.Parameter(torch.randn(input_dof, output_dof, dtype=torch.complex128))
        self.input_dof = input_dof

    def forward(self, field):
        out = torch.matmul(field, self.weights)
        return out

    # Starts as dupe Matrix, because default pe is already close to goal


class ReducedNonGaugeLinear(nn.Module):
    def __init__(self, input_size, output_size):
        super(ReducedNonGaugeLinear, self).__init__()
        assert input_size % 4 == 0, 'input_size must be divisible by 4'

        block_width = input_size // 4
        block_height = output_size // 4

        # Create matrix-blocks (start with one)
        block = torch.zeros(block_width, block_height, dtype=torch.complex128)
        for index in range(output_size // 4):
            block[index % block_width][index] = 1

        # Create the diagonal block matrix
        block_matrix = torch.zeros(input_size, output_size, dtype=torch.complex128)
        block_matrix[:block_width, :block_height] = block
        block_matrix[block_width:2 * block_width, block_height:2 * block_height] = block
        block_matrix[2 * block_width:3 * block_width, 2 * block_height:3 * block_height] = block
        block_matrix[3 * block_width:4 * block_width, 3 * block_height:4 * block_height] = block

        self.weights = nn.Parameter(block_matrix)

    def forward(self, field):
        out = torch.matmul(field, self.weights)
        return out

    # Volume size spin matirx (diffrent at each site)


class LocalNonGaugeLinear(nn.Module):
    def __init__(self, input_dof, lattice):
        super(LocalNonGaugeLinear, self).__init__()
        self.weights = nn.Parameter(torch.randn(*lattice, input_dof, input_dof, dtype=torch.complex128))

    def forward(self, field):
        out = torch.matmul(field, self.weights)
        return out

    # Volume size Matrix which transforms Values based on relative shift compared to Queries


class DirectionBasedLinear(nn.Module):
    def __init__(self, non_gauge_dof, lattice):
        super(DirectionBasedLinear, self).__init__()
        self.volume = 1
        for num in lattice:
            self.volume *= num
        local_spin_matrix = torch.randn(*lattice, non_gauge_dof, non_gauge_dof, dtype=torch.complex128)
        local_spin_matrix = local_spin_matrix.expand(*lattice, *lattice, non_gauge_dof, non_gauge_dof)
        X_dim, Y_dim, Z_dim, T_dim = lattice
        range_lattice = torch.arange(X_dim), torch.arange(Y_dim), torch.arange(Z_dim), torch.arange(T_dim)
        x_x, y_x, z_x, t_x, x_y, y_y, z_y, t_y = torch.meshgrid(*range_lattice, *range_lattice, indexing="ij")
        x_y_new, y_y_new, z_y_new, t_y_new = (x_x - x_y) % lattice[0], (y_x - y_y) % lattice[1], (
                z_x - z_y) % lattice[2], (t_x - t_y) % lattice[3]
        local_spin_matrix = local_spin_matrix[x_x, y_x, z_x, t_x, x_y_new, y_y_new, z_y_new, t_y_new]

        self.weights = nn.Parameter(local_spin_matrix.reshape(self.volume, self.volume, non_gauge_dof, non_gauge_dof))

    def forward(self, field):
        out = field.reshape(field.shape[0], self.volume, *field.shape[-2:])
        out = torch.einsum('Nmis,nmsj->Nnmij', [out, self.weights])
        return out


class PTC(nn.Module):
    def __init__(self, input_dof, output_dof, path_list, gauge_field):
        super(PTC, self).__init__()
        self.PTC_layers = nn.ModuleList(
            [nn.Sequential(NonGaugeLinear(input_dof, output_dof), T(p, gauge_field)) for p
             in path_list])

    def forward(self, field):
        out = self.PTC_layers[0](field)
        for index in range(1, len(self.PTC_layers)):
            out += self.PTC_layers[index](field)
        # print("PTC mean: ", torch.mean(torch.abs(out)))
        return out

    def gauge_tra(self, new_gauge):
        for layer in self.PTC_layers:
            layer[1].gauge_tra(new_gauge)

        # Quadriples input field and adds a four block PE, each block representing one Dimension


class PE_4D(nn.Module):
    def __init__(self, gauge_field, input_non_gauge_dof):
        super(PE_4D, self).__init__()
        X_dim, Y_dim, Z_dim, T_dim = gauge_field.shape[1:-2]
        gauge_dof = gauge_field.shape[-1]

        x_indices = torch.arange(X_dim, dtype=torch.float32)
        t_indices = torch.arange(T_dim, dtype=torch.float32)
        k_indices = torch.arange(2, input_non_gauge_dof + 2, dtype=torch.float32)

        # Compute values using vectorized operations
        x_block_even = torch.sin(2 * x_indices.unsqueeze(-1) / X_dim ** (k_indices[::2] / input_non_gauge_dof))
        t_block_even = torch.sin(2 * t_indices.unsqueeze(-1) / T_dim ** (k_indices[::2] / input_non_gauge_dof))
        x_block_odd = torch.cos(2 * x_indices.unsqueeze(-1) / X_dim ** ((k_indices[1::2] - 1) / input_non_gauge_dof))
        t_block_odd = torch.cos(2 * t_indices.unsqueeze(-1) / T_dim ** ((k_indices[1::2] - 1) / input_non_gauge_dof))

        # Combine even and odd values
        x_block = torch.zeros(X_dim, gauge_dof, input_non_gauge_dof, dtype=torch.complex128)
        t_block = torch.zeros(T_dim, gauge_dof, input_non_gauge_dof, dtype=torch.complex128)
        x_block[:, :, ::2] = x_block_even.unsqueeze(1)
        t_block[:, :, ::2] = t_block_even.unsqueeze(1)
        x_block[:, :, 1::2] = x_block_odd.unsqueeze(1)
        t_block[:, :, 1::2] = t_block_odd.unsqueeze(1)

        index_x, index_y, index_z, index_t = torch.meshgrid(torch.arange(X_dim),
                                                            torch.arange(Y_dim),
                                                            torch.arange(Z_dim),
                                                            torch.arange(T_dim), indexing="ij")

        self.pe_tensor = torch.cat((x_block[index_x],
                                    x_block[index_y],
                                    x_block[index_z],
                                    t_block[index_t]), dim=-1)

        self.dupe_tensor = torch.zeros(input_non_gauge_dof, 4 * input_non_gauge_dof, dtype=torch.complex128)
        for index_j in range(4 * input_non_gauge_dof):
            self.dupe_tensor[index_j % input_non_gauge_dof][index_j] = 1

        self.dominance = nn.Parameter(torch.tensor(1, dtype=torch.complex128), requires_grad=True)

    def forward(self, field):
        out = torch.matmul(field, self.dupe_tensor)
        out = out + self.pe_tensor.unsqueeze(0) * self.dominance
        return out

    def gauge_tra(self, new_gauge):
        self.pe_tensor = gauge_tra(self.pe_tensor, new_gauge)

        # Direction - Based linear + Supergaugefield


class SuperPtc(nn.Module):
    def __init__(self, gauge_field, input_non_gauge_dof):
        super(SuperPtc, self).__init__()
        lattice = gauge_field.shape[1:-2]
        self.volume = 1
        for num in lattice:
            self.volume *= num
        self.gauge_field = gauge_field
        temp = make_super_gauge_field(gauge_field)
        self.super_gauge_field = temp.reshape(self.volume, self.volume, *gauge_field.shape[-2:])
        self.linear = DirectionBasedLinear(input_non_gauge_dof, lattice)

    def forward(self, field):
        out = field.reshape(field.shape[0], self.volume, *field.shape[-2:])
        out = self.linear(out)
        out = torch.einsum('nmis,Nnmsj->Nnij', [self.super_gauge_field, out]).reshape(*field.shape)
        # print("SuperPtc mean: ", torch.mean(torch.abs(out)))
        return out

    def gauge_tra(self, new_gauge):
        self.gauge_field = gauge_tra(self.gauge_field, new_gauge, field_is_gauge_field=True)
        temp = make_super_gauge_field(self.gauge_field)
        self.super_gauge_field = temp.reshape(self.volume, self.volume, *self.gauge_field.shape[-2:])

        # Core of the SA mechanism. Converts Attention field (3x3 Matrices) into scalar filed using covarient nature of the Trace
        # finally converting Attention into percentage using Softmax and scaling the Supergaugefield with them


class TraceActivation(nn.Module):
    def __init__(self, volume, gauge_field, show_time):
        super(TraceActivation, self).__init__()
        self.volume = volume

        start_time_sgf = time.time()
        temp = make_super_gauge_field(gauge_field)
        self.super_gauge_field = temp.reshape(self.volume, self.volume, *gauge_field.shape[-2:])
        self.activation = nn.Softmax(dim=-1)
        end_time = time.time()
        execution_time = end_time - start_time_sgf
        print(f"SuperGaugeField in: {execution_time * 1e3:.3f} ms") if show_time else None

    def forward(self, attention):
        matrix_values = torch.matmul(attention, dagger(attention))
        matrix_values = torch.abs(torch.sum(torch.diagonal(matrix_values, dim1=-2, dim2=-1), dim=-1))
        matrix_values = matrix_values ** 0.5
        effect = self.activation(matrix_values)
        # print(torch.sum(effect[0,0]))
        # print("Attention Matrix: ", effect.reshape(1, 4,4,4,8, 4,4,4,8)[0, 0,0,1,4, 0,0,:,:])
        out = torch.einsum('nmij,Nnm->Nnmij', [self.super_gauge_field, effect])
        return out

    def gauge_tra(self, tra_gauge):
        temp = make_super_gauge_field(tra_gauge)
        self.super_gauge_field = temp.reshape(self.volume, self.volume, *tra_gauge.shape[-2:])

        # calculates simelarity between queries and keys (based on how orthogonal they are) than adds values scaled with
        # thier respective simelarity


class SelfAttention(nn.Module):
    def __init__(self, gauge_field, show_time):
        super(SelfAttention, self).__init__()
        self.gauge_field = gauge_field
        lattice = gauge_field.shape[1:-2]
        self.volume = 1
        for dim_size in lattice:
            self.volume *= dim_size
        self.activation = TraceActivation(self.volume, self.gauge_field, show_time)

    def forward(self, queries, keys, values, show_time):
        queries_shape = queries.shape
        keys_shape = keys.shape
        queries = queries.reshape(queries_shape[0], self.volume, *queries_shape[-2:])
        keys = keys.reshape(keys_shape[0], self.volume, *keys_shape[-2:])

        start_time_attention = time.time()
        attention = torch.einsum("Nnis,Nmjs->Nnmij", [queries, torch.conj(keys)])
        end_time = time.time()
        execution_time = end_time - start_time_attention
        print(f"Q * dagger(K) in: {execution_time * 1e3:.3f} ms") if show_time else None

        start_time_activation = time.time()
        attention = self.activation(attention / queries_shape[-1])
        end_time = time.time()
        execution_time = end_time - start_time_activation
        print(f"Activation in: {execution_time * 1e3:.3f} ms") if show_time else None

        start_time_add = time.time()
        out = torch.einsum("Nnmis,Nnmsj->Nnij", [attention, values])
        end_time = time.time()
        execution_time = end_time - start_time_add
        print(f"Transforming and Adding Values in: {execution_time * 1e3:.3f} ms") if show_time else None

        out = out.reshape(*queries_shape[:-1], -1)
        return out

    def gauge_tra(self, new_gauge):
        self.gauge_field = gauge_tra(self.gauge_field, new_gauge, field_is_gauge_field=True)
        self.activation.gauge_tra(self.gauge_field)

        # Trasformer creates queries and keys with 4 - Block style linears (each block representing a Dimension of the lattice)
        # values are created using directin based linear to mimic the operations in DW_C


class Transformer(nn.Module):
    def __init__(self, gauge_field, input_non_gauge_dof, linear_size, norm, show_time=False):
        super(Transformer, self).__init__()
        start_time = time.time()
        lattice = gauge_field.shape[1:-2]
        lattice_dof = len(lattice)
        pe_output_size = lattice_dof * input_non_gauge_dof
        self.linear_size = linear_size
        self.norm = norm

        assert linear_size & lattice_dof == 0, "linear Size must be dividable by 4!"

        # Transformations
        self.norm_field_scale = nn.Parameter(torch.tensor(7, dtype=torch.complex128))

        start_time_pe = time.time()
        self.pe = PE_4D(gauge_field, input_non_gauge_dof)
        end_time = time.time()
        execution_time = end_time - start_time_pe
        print(f"PE in: {execution_time*1e3:.3f} ms") if show_time else None

        self.W_Q = ReducedNonGaugeLinear(pe_output_size, linear_size)
        self.W_K = ReducedNonGaugeLinear(pe_output_size, linear_size)

        start_time_dir = time.time()
        self.W_V = DirectionBasedLinear(input_non_gauge_dof, lattice)
        end_time = time.time()
        execution_time = end_time - start_time_dir
        print(f"DirectionBasedLinear in: {execution_time * 1e3:.3f} ms") if show_time else None

        self.self_attention = SelfAttention(gauge_field, show_time)
        end_time = time.time()
        execution_time = end_time - start_time
        print(f"Transformer in: {execution_time*1e3:.3f} ms") if show_time else None

    def forward(self, field, show_time=False):
        if self.norm:
            dims = tuple(range(1, len(field.shape)))
            norm_field = self.norm_field_scale * field / torch.sum(torch.matmul(dagger(field), field), dim=dims) ** 0.5
        else:
            norm_field = field

        start_time = time.time()
        queries = self.W_Q(self.pe(norm_field))
        keys = self.W_K(self.pe(norm_field))
        values = self.W_V(field)
        end_time = time.time()
        execution_time = end_time - start_time
        print(f"Q,K,V in: {execution_time*1e3:.3f} ms") if show_time else None

        # print("Values Mean: ", torch.mean(torch.abs(values)))
        out = self.self_attention(queries, keys, values, show_time)
        # print("SA mean: ", torch.mean(torch.abs(out)))
        end_time = time.time()
        execution_time = end_time - start_time
        print(f"Everything in: {execution_time*1e3:.3f} ms") if show_time else None

        return out

    def gauge_tra(self, new_gauge):
        self.pe.gauge_tra(new_gauge)
        self.self_attention.gauge_tra(new_gauge)
